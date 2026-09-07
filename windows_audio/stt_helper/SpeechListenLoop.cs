using System.Media;
using System.Text;
using System.Text.RegularExpressions;
using Windows.Globalization;
using Windows.Media.SpeechRecognition;
using WinRtRecognizer = Windows.Media.SpeechRecognition.SpeechRecognizer;

namespace SttHelper;

/// <summary>
/// Macchina a stati WinRT per un singolo <c>POST /listen</c> (che può restare
/// aperto attraverso più cicli wake → dettatura → annulla).
///
/// Sequenza (stesso microfono, un SpeechRecognizer per volta, mai ricompilare
/// una list-constraint in grammatica libera: WinRT risponde HRESULT senza testo):
/// <list type="number">
/// <item>List constraint sulla wake di apertura (default «ehi assistente»).</item>
/// <item>Match → beep Asterisk, Dispose, nuovo SpeechRecognizer, topic Dictation.</item>
/// <item>Il silenzio NON chiude il turno: chiude «assistente chiudi» o il timer 45s.</item>
/// <item>Sul testo, prima «assistente annulla» poi «assistente chiudi».</item>
/// <item>Dopo 45s: se c'è testo lo spedisce; se il buffer è vuoto torna alla wake.</item>
/// </list>
///
/// Grammatica «default» (nessun constraint) su questa macchina compilava Success
/// e subito dopo UserCanceled senza hypotesis: non la usiamo. Topic Dictation
/// poi WebSearch, sempre su un riconoscitore nuovo. UserCanceled in dettatura
/// non riavvia la stessa session: passa alla grammatica successiva o torna alla wake.
///
/// Tutte le API WinRT partono dallo STA della <see cref="HiddenStaForm"/>.
/// I callback WinRT sono <c>ThreadingModel.Both</c>: possono arrivare sul pool;
/// li ributtiamo sulla form con <see cref="HiddenStaForm.PostToSta"/>.
/// Non usare <c>ConfigureAwait(false)</c> qui: perderemmo lo STA dopo il primo await.
/// </summary>
internal sealed class SpeechListenLoop
{
    private const string CultureName = "it-IT";

    // Topic cloud, in quest'ordine. Niente grammatica vuota (UserCanceled senza testo).
    private static readonly (string Label, SpeechRecognitionScenario Scenario, string Tag)[] DictationGrammars =
    [
        ("topic Dictation", SpeechRecognitionScenario.Dictation, "dictation"),
        ("topic WebSearch", SpeechRecognitionScenario.WebSearch, "webSearch"),
    ];

    // HWND + pump: ogni chiamata WinRT parte da qui (PostToSta / await sullo STA).
    private readonly HiddenStaForm _form;
    private readonly HelperSettings _settings;
    // Timer WinForms: i Tick arrivano già sullo STA, niente marshal extra.
    private readonly System.Windows.Forms.Timer _dictationTimer;

    // Un riconoscitore: wake list XOR dettatura topic, oggetti distinti (Dispose in mezzo).
    private WinRtRecognizer? _recognizer;
    // Quale grammatica topic stiamo usando in dettatura (0 = Dictation, 1 = WebSearch).
    private int _dictationGrammarIndex;
    // Testo già finalizzato (ResultGenerated con testo, anche Low/Rejected).
    private readonly StringBuilder _committed = new();
    // Hypotesis corrente: sostituisce se stessa, non si concatena (è la frase in corso).
    private string _hypothesis = "";
    // True mentre StopAsync è voluto da noi: Completed non deve riarmare la sessione.
    private bool _stopping;
    // Generazione: un /listen preemptato incrementa, gli eventi stantii si scartano.
    private int _runId;
    // Completa il POST /listen HTTP (thread pool in attesa). Mai con stringa vuota.
    private TaskCompletionSource? _listenTcs;
    // Completa la sola fase «aspetta ehi assistente».
    private TaskCompletionSource<bool>? _wakeTcs;
    // Completa la fase dettatura: chiudi / annulla / timeout.
    private TaskCompletionSource<DictationOutcome>? _dictationTcs;
    // Token del /listen corrente (preempt o shutdown).
    private CancellationToken _listenCancel;
    private ListenPhase _phase = ListenPhase.Idle;

    public SpeechListenLoop(HiddenStaForm form, HelperSettings settings)
    {
        _form = form;
        _settings = settings;
        _dictationTimer = new System.Windows.Forms.Timer
        {
            Interval = HelperSettings.DictationTimeoutSeconds * 1000,
        };
        // Un solo colpo: il Tick si auto-disarma. Non è un heartbeat.
        _dictationTimer.Tick += (_, _) =>
        {
            _dictationTimer.Stop();
            OnDictationTimeoutSta();
        };
    }

    /// <summary>
    /// Avvia (o riavvia) un ciclo listen. Chiamabile dal thread pool HTTP.
    /// Completa quando la wake è riconosciuta (beep fatto, mic rilasciato).
    /// Il transcript della dettatura lo fa il host Python (Vosk): WinRT topic
    /// su questo processo desktop chiude con UserCanceled senza testo.
    /// </summary>
    public Task ListenAsync(CancellationToken cancellationToken)
    {
        var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        // Linked: cancel HTTP (preempt) o Shutdown della form.
        var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);

        _form.PostToSta(() =>
        {
            _ = PreemptAndRunStaAsync(tcs, linked);
        });

        return tcs.Task;
    }

    /// <summary>
    /// Chiusura processo: ferma timer e riconoscitore sullo STA. Non aspetta.
    /// </summary>
    public void Shutdown()
    {
        _form.PostToSta(() =>
        {
            _runId++;
            _dictationTimer.Stop();
            _listenTcs?.TrySetCanceled();
            _wakeTcs?.TrySetCanceled();
            _dictationTcs?.TrySetCanceled();
            _ = TearDownRecognizerAsync();
        });
    }

    private async Task PreemptAndRunStaAsync(
        TaskCompletionSource tcs,
        CancellationTokenSource linked)
    {
        // Nuova generazione prima dello Stop: gli eventi in volo vedono l'id vecchio e si fermano.
        _runId++;
        var myId = _runId;
        _dictationTimer.Stop();
        // Il TCS precedente è del HTTP vecchio: 503, non transcript vuoto.
        _listenTcs?.TrySetCanceled();
        _wakeTcs?.TrySetCanceled();
        _dictationTcs?.TrySetCanceled();
        await TearDownRecognizerAsync();
        if (myId != _runId)
        {
            // Un altro /listen è arrivato mentre smontavamo: questo TCS non è più il titolare.
            tcs.TrySetCanceled();
            linked.Dispose();
            return;
        }

        _listenTcs = tcs;
        _listenCancel = linked.Token;
        _phase = ListenPhase.Idle;
        try
        {
            await RunSessionStaAsync(myId);
        }
        catch (OperationCanceledException)
        {
            tcs.TrySetCanceled(_listenCancel);
        }
        catch (Exception ex)
        {
            HelperLog.Info($"Sessione listen fallita: {ex}");
            tcs.TrySetException(ex);
        }
        finally
        {
            _dictationTimer.Stop();
            await TearDownRecognizerAsync();
            if (ReferenceEquals(_listenTcs, tcs))
                _listenTcs = null;
            linked.Dispose();
        }
    }

    private async Task RunSessionStaAsync(int runId)
    {
        // Loop wake→dettatura finché non c'è un transcript da spedire (chiudi o timeout con testo).
        while (!_listenCancel.IsCancellationRequested && runId == _runId)
        {
            HelperLog.Info("Fase wake apertura: list constraint.");
            var woke = await WaitForOpenWakeStaAsync(runId);
            if (!woke || runId != _runId || _listenCancel.IsCancellationRequested)
                return;

            // Beep di conferma: l'utente sa che può dettare. Asterisk ≠ Hand dell'annulla.
            SystemSounds.Asterisk.Play();
            // Pausa breve: il beep non deve finire nel primo chunk di dictation.
            await Task.Delay(700);
            if (runId != _runId || _listenCancel.IsCancellationRequested)
                return;

            HelperLog.Info("Wake ok: smonto WinRT e lascio il mic a Vosk (host Python).");
            _listenTcs?.TrySetResult();
            return;
        }

        _listenTcs?.TrySetCanceled(_listenCancel);
    }

    private async Task<bool> WaitForOpenWakeStaAsync(int runId)
    {
        _phase = ListenPhase.WakeOpen;
        _wakeTcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        // Buffer della dettatura precedente (annulla / timeout vuoto): non deve restare in giro.
        _committed.Clear();
        _hypothesis = "";

        try
        {
            await CreateRecognizerStaAsync(RecognizerKind.WakeOpen);
            // Preempt durante Compile/Start: questo ciclo non è più il titolare.
            if (runId != _runId)
                return false;

            using var reg = _listenCancel.Register(
                () => _form.PostToSta(() => _wakeTcs?.TrySetCanceled(_listenCancel)));
            var woke = await _wakeTcs.Task;
            // Durante beep/pausa non siamo più in list-constraint: ignora result stantii.
            if (woke)
                _phase = ListenPhase.Idle;
            return woke;
        }
        finally
        {
            _wakeTcs = null;
            // Dispose obbligatorio: ricompilare list → topic sullo stesso oggetto
            // lancia HRESULT senza messaggio (log: «Impossibile trovare il testo…»).
            await TearDownRecognizerAsync();
        }
    }

    private async Task<DictationOutcome> RunDictationStaAsync(int runId)
    {
        _phase = ListenPhase.Dictation;
        // Nuovo turno: l'annulla precedente non deve riapparire nel transcript.
        _committed.Clear();
        _hypothesis = "";
        // Ogni turno riparte da topic Dictation; UserCanceled può salire a WebSearch.
        _dictationGrammarIndex = 0;
        _dictationTcs = new TaskCompletionSource<DictationOutcome>(
            TaskCreationOptions.RunContinuationsAsynchronously);

        try
        {
            // Sempre un SpeechRecognizer nuovo: la wake è già stata smontata.
            await CreateRecognizerStaAsync(RecognizerKind.Dictation);
            if (runId != _runId)
                return DictationOutcome.Canceled();

            _dictationTimer.Stop();
            _dictationTimer.Start();

            using var reg = _listenCancel.Register(
                () => _form.PostToSta(() => _dictationTcs?.TrySetCanceled(_listenCancel)));

            return await _dictationTcs.Task;
        }
        finally
        {
            _dictationTimer.Stop();
            _dictationTcs = null;
            // Fine turno: smonta tutto. La wake successiva ricrea la list-constraint.
            await TearDownRecognizerAsync();
        }
    }

    private async Task CreateRecognizerStaAsync(RecognizerKind kind)
    {
        // Dispose del precedente prima del ctor: due SpeechRecognizer sullo stesso mic falliscono.
        await TearDownRecognizerAsync();

        var language = new Language(CultureName);
        WinRtRecognizer recognizer;
        try
        {
            recognizer = new WinRtRecognizer(language);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException(
                "Impossibile creare SpeechRecognizer it-IT. Installa il pacchetto lingua italiano " +
                "e attiva il riconoscimento vocale online in Impostazioni Windows.",
                ex);
        }

        _recognizer = recognizer;
        StretchSilenceTimeouts(recognizer, kind);

        recognizer.Constraints.Clear();
        if (kind == RecognizerKind.WakeOpen)
        {
            // Lista: sveglia selettiva. «hei» è la forma parlata di «ehi» (WinRT it-IT).
            var openPhrases = new List<string> { _settings.WakeOpen };
            var extraHei = PhraseMatcher.HeiAlias(_settings.WakeOpen);
            if (extraHei is not null)
                openPhrases.Add(extraHei);
            recognizer.Constraints.Add(
                new SpeechRecognitionListConstraint(openPhrases, "wake-open"));
            var compiled = await recognizer.CompileConstraintsAsync();
            if (compiled.Status != SpeechRecognitionResultStatus.Success)
            {
                throw new InvalidOperationException(
                    $"CompileConstraintsAsync={compiled.Status}. Per la wake serve la lingua it-IT.");
            }

            HelperLog.Info($"CompileConstraintsAsync ok kind={kind} status={compiled.Status}.");
        }
        else
        {
            await CompileDictationGrammarStaAsync(recognizer);
        }

        // Hypotesis: testo parziale. Sulla wake serve per ContainsPhrase anche a metà frase.
        recognizer.HypothesisGenerated += OnHypothesisGenerated;
        recognizer.ContinuousRecognitionSession.ResultGenerated += OnResultGenerated;
        recognizer.ContinuousRecognitionSession.Completed += OnSessionCompleted;
        try
        {
            await recognizer.ContinuousRecognitionSession.StartAsync(
                SpeechContinuousRecognitionMode.Default);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException(
                "StartAsync del riconoscimento continuo fallito. Controlla il microfono e il consenso privacy. " +
                DescribeWinRt(ex),
                ex);
        }
    }

    /// <summary>
    /// Topic cloud, mai grammatica vuota: sul tuo log il default compilava e poi
    /// UserCanceled senza hypotesis. Parte da <see cref="_dictationGrammarIndex"/>.
    /// </summary>
    private async Task CompileDictationGrammarStaAsync(WinRtRecognizer recognizer)
    {
        Exception? last = null;
        for (var i = _dictationGrammarIndex; i < DictationGrammars.Length; i++)
        {
            var (label, scenario, tag) = DictationGrammars[i];
            recognizer.Constraints.Clear();
            recognizer.Constraints.Add(new SpeechRecognitionTopicConstraint(scenario, tag));
            try
            {
                var compiled = await recognizer.CompileConstraintsAsync();
                HelperLog.Info($"Compile dettatura '{label}': {compiled.Status}");
                if (compiled.Status == SpeechRecognitionResultStatus.Success)
                {
                    _dictationGrammarIndex = i;
                    HelperLog.Info($"Dettatura WinRT it-IT con grammatica: {label}.");
                    return;
                }

                last = new InvalidOperationException($"Compile '{label}' = {compiled.Status}");
            }
            catch (Exception ex)
            {
                HelperLog.Info($"Compile dettatura '{label}' eccezione: {DescribeWinRt(ex)}");
                last = ex;
            }
        }

        throw new InvalidOperationException(
            "Nessuna grammatica topic it-IT ha compilato. Impostazioni, Privacy e sicurezza, " +
            "Comandi vocali: riconoscimento vocale online acceso, e rete verso Microsoft.",
            last);
    }

    private static string DescribeWinRt(Exception ex)
    {
        // WinRT spesso mette «Impossibile trovare il testo associato a questo codice»
        // al posto del messaggio: l'HRESULT è l'unica traccia.
        return $"{ex.GetType().Name} hr=0x{ex.HResult:X8} {ex.Message}";
    }

    /// <summary>
    /// UserCanceled ha ucciso la session: stesso oggetto non si riavvia. Nuovo
    /// riconoscitore, grammatica successiva. Il timer 45s resta quello del turno.
    /// </summary>
    private async Task RecreateDictationAfterUserCanceledStaAsync()
    {
        if (_phase != ListenPhase.Dictation)
            return;
        if (_dictationTcs is null || _dictationTcs.Task.IsCompleted)
            return;
        if (_listenCancel.IsCancellationRequested)
            return;
        try
        {
            await CreateRecognizerStaAsync(RecognizerKind.Dictation);
        }
        catch (Exception ex)
        {
            HelperLog.Info($"Ricreo dettatura dopo UserCanceled: {DescribeWinRt(ex)}");
            _dictationTcs?.TrySetException(ex);
        }
    }

    /// <summary>
    /// Wake: silenzio lungo, aspettiamo solo la frase. Dettatura: EndSilence ~2s così
    /// ResultGenerated emette le frasi; AutoStop lunghissimo così il tetto è il timer 45s.
    /// </summary>
    private static void StretchSilenceTimeouts(WinRtRecognizer recognizer, RecognizerKind kind)
    {
        TimeSpan[] endAndBabble;
        TimeSpan[] initial;
        if (kind == RecognizerKind.Dictation)
        {
            endAndBabble =
            [
                TimeSpan.FromSeconds(2),
                TimeSpan.FromSeconds(1.5),
                TimeSpan.FromSeconds(1),
            ];
            initial =
            [
                TimeSpan.FromSeconds(15),
                TimeSpan.FromSeconds(10),
                TimeSpan.FromSeconds(5),
            ];
        }
        else
        {
            endAndBabble =
            [
                TimeSpan.FromMinutes(5),
                TimeSpan.FromMinutes(1),
                TimeSpan.FromSeconds(30),
                TimeSpan.FromSeconds(10),
                TimeSpan.FromSeconds(5),
            ];
            initial = endAndBabble;
        }

        TrySetMax(t => recognizer.Timeouts.EndSilenceTimeout = t, endAndBabble);
        TrySetMax(t => recognizer.Timeouts.InitialSilenceTimeout = t, initial);
        TrySetMax(t => recognizer.Timeouts.BabbleTimeout = t, endAndBabble);

        var autoStop = new[]
        {
            TimeSpan.FromDays(1),
            TimeSpan.FromHours(1),
            TimeSpan.FromMinutes(5),
        };
        // Senza questo la session continua muore dopo ~15s di silenzio, aggirando EndSilenceTimeout.
        TrySetMax(t => recognizer.ContinuousRecognitionSession.AutoStopSilenceTimeout = t, autoStop);
    }

    private static void TrySetMax(Action<TimeSpan> setter, TimeSpan[] candidates)
    {
        foreach (var candidate in candidates)
        {
            try
            {
                setter(candidate);
                return;
            }
            catch (ArgumentException)
            {
                // Valore oltre il tetto di questa build: prova il successivo più corto.
                // Copre anche ArgumentOutOfRangeException (ne è derivata): un catch
                // dedicato dopo questo è CS0160 e impedisce la compilazione.
            }
            catch (System.Runtime.InteropServices.COMException)
            {
                // WinRT a volte rifiuta con E_INVALIDARG COM, non ArgumentException.
            }
        }
    }

    private void OnHypothesisGenerated(WinRtRecognizer sender, SpeechRecognitionHypothesisGeneratedEventArgs args)
    {
        // Pool thread possibile: catturiamo il testo e lo elaboriamo sulla form.
        var text = args.Hypothesis?.Text ?? "";
        _form.PostToSta(() => OnPartialTextSta(text, isFinal: false, SpeechRecognitionConfidence.Medium));
    }

    private void OnResultGenerated(
        SpeechContinuousRecognitionSession sender,
        SpeechContinuousRecognitionResultGeneratedEventArgs args)
    {
        var result = args.Result;
        var text = result?.Text ?? "";
        var confidence = result?.Confidence ?? SpeechRecognitionConfidence.Rejected;
        var status = result?.Status;
        HelperLog.Info($"ResultGenerated status={status} conf={confidence} text='{text}'");
        // isFinal=true: questo testo sostituisce l'hypotesis della stessa frase.
        _form.PostToSta(() => OnPartialTextSta(text, isFinal: true, confidence));
    }

    private void OnSessionCompleted(
        SpeechContinuousRecognitionSession sender,
        SpeechContinuousRecognitionCompletedEventArgs args)
    {
        var status = args.Status;
        _form.PostToSta(() => OnSessionCompletedSta(status));
    }

    private void OnPartialTextSta(string text, bool isFinal, SpeechRecognitionConfidence confidence)
    {
        // Senza riconoscitore non c'è match né dettatura.
        if (_stopping || _recognizer is null)
            return;

        if (_phase == ListenPhase.WakeOpen)
        {
            if (_recognizer is null)
                return;

            // List constraint: di solito il result è esattamente la frase. Accettiamo anche Contains.
            if (PhraseMatcher.ContainsPhrase(text, _settings.WakeOpen)
                || PhraseMatcher.HeiAlias(_settings.WakeOpen) is { } hei
                    && PhraseMatcher.ContainsPhrase(text, hei))
            {
                HelperLog.Info("Wake apertura riconosciuta.");
                _wakeTcs?.TrySetResult(true);
            }

            return;
        }

        if (_phase != ListenPhase.Dictation)
            return;

        // Eventi in ritardo dopo timeout/chiudi: il TCS è già chiuso, non toccare il buffer.
        if (_dictationTcs is null || _dictationTcs.Task.IsCompleted)
            return;

        if (!isFinal)
            HelperLog.Info($"Dettatura hypotesis: '{text}'");

        // Snapshot da ispezionare: l'hypotesis è la frase in corso; il result la sostituisce, non si somma.
        string snapshot;
        if (isFinal)
        {
            snapshot = Combine(_committed.ToString(), text);
        }
        else
        {
            _hypothesis = text;
            snapshot = Combine(_committed.ToString(), _hypothesis);
        }

        // Ordine vincolato dal piano: annulla prima di chiudi, stesso passaggio di hypotesis.
        if (PhraseMatcher.ContainsPhrase(snapshot, _settings.WakeCancel)
            || PhraseMatcher.ContainsPhrase(text, _settings.WakeCancel))
        {
            HelperLog.Info("Wake annulla nel testo.");
            _dictationTcs?.TrySetResult(DictationOutcome.Cancel());
            return;
        }

        if (PhraseMatcher.ContainsPhrase(snapshot, _settings.WakeClose)
            || PhraseMatcher.ContainsPhrase(text, _settings.WakeClose))
        {
            var transcript = PhraseMatcher.StripPhrase(snapshot, _settings.WakeClose);
            HelperLog.Info("Wake chiusura nel testo.");
            // Solo «assistente chiudi» senza comando: non spedire "" (il loop WSL uscirebbe).
            if (string.IsNullOrWhiteSpace(transcript))
                _dictationTcs?.TrySetResult(DictationOutcome.TimeoutEmpty());
            else
                _dictationTcs?.TrySetResult(DictationOutcome.Close(transcript));
            return;
        }

        if (!isFinal)
            return;

        // Testo c'è: lo teniamo anche se Confidence è Low/Rejected. Il giro RecognizeAsync
        // scartava Rejected e il buffer restava vuoto 45s.
        if (string.IsNullOrWhiteSpace(text))
        {
            HelperLog.Info($"Result senza testo conf={confidence}");
            return;
        }

        HelperLog.Info($"Dettatura commit conf={confidence} text='{text}'");
        if (_committed.Length > 0)
            _committed.Append(' ');
        _committed.Append(text.Trim());
        _hypothesis = "";
    }

    private void OnDictationTimeoutSta()
    {
        if (_phase != ListenPhase.Dictation || _stopping)
            return;

        var combined = Combine(_committed.ToString(), _hypothesis).Trim();
        if (combined.Length > 0)
        {
            HelperLog.Info("Timeout 45s con testo: chiusura implicita.");
            _dictationTcs?.TrySetResult(DictationOutcome.TimeoutWithText(combined));
        }
        else
        {
            HelperLog.Info("Timeout 45s buffer vuoto.");
            _dictationTcs?.TrySetResult(DictationOutcome.TimeoutEmpty());
        }
    }

    private void OnSessionCompletedSta(SpeechRecognitionResultStatus status)
    {
        if (_stopping)
            return;

        HelperLog.Info($"ContinuousRecognitionSession.Completed status={status} phase={_phase}");

        // Se abbiamo già deciso (chiudi / annulla / timeout), TearDown sta arrivando: non riarmare.
        if (_phase == ListenPhase.WakeOpen && _wakeTcs?.Task.IsCompleted == true)
            return;
        if (_phase == ListenPhase.Dictation && _dictationTcs?.Task.IsCompleted == true)
            return;

        if (status == SpeechRecognitionResultStatus.UserCanceled
            && _phase == ListenPhase.Dictation)
        {
            // Non è «assistente annulla»: Windows ha chiuso la session (privacy/rete/topic).
            // Riavviare la stessa session lascia il mic morto fino al timer 45s.
            HelperLog.Info(
                "Dettatura UserCanceled: Windows ha chiuso l'ascolto libero " +
                "(Comandi vocali / rete). Non è la wake di annulla.");
            var next = _dictationGrammarIndex + 1;
            if (next < DictationGrammars.Length)
            {
                _dictationGrammarIndex = next;
                HelperLog.Info($"Riprovo dettatura con grammatica indice {next} su riconoscitore nuovo.");
                _ = RecreateDictationAfterUserCanceledStaAsync();
            }
            else
            {
                HelperLog.Info("UserCanceled: topic esauriti, buffer vuoto, torno alla wake.");
                _dictationTcs?.TrySetResult(DictationOutcome.TimeoutEmpty());
            }

            return;
        }

        if (status == SpeechRecognitionResultStatus.NetworkFailure
            || status == SpeechRecognitionResultStatus.TopicLanguageNotSupported)
        {
            var ex = new InvalidOperationException(
                $"Riconoscimento interrotto ({status}). Per la dettatura it-IT servono rete e " +
                "pacchetto lingua / riconoscimento vocale online.");
            if (_phase == ListenPhase.WakeOpen)
                _wakeTcs?.TrySetException(ex);
            else if (_phase == ListenPhase.Dictation)
                _dictationTcs?.TrySetException(ex);
            return;
        }

        // AutoStopSilence o timeout WinRT nonostante i nostri Stretch: riarmiamo senza perdere il buffer.
        if (_recognizer is null)
            return;

        _ = RestartSessionAfterUnexpectedStopStaAsync();
    }

    private async Task RestartSessionAfterUnexpectedStopStaAsync()
    {
        if (_stopping || _recognizer is null)
            return;
        // Doppio check: il TCS può completarsi mentre questo metodo era già in coda STA.
        if (_phase == ListenPhase.WakeOpen && _wakeTcs?.Task.IsCompleted == true)
            return;
        if (_phase == ListenPhase.Dictation && _dictationTcs?.Task.IsCompleted == true)
            return;
        try
        {
            if (_recognizer.State == SpeechRecognizerState.Idle)
                await _recognizer.ContinuousRecognitionSession.StartAsync(SpeechContinuousRecognitionMode.Default);
        }
        catch (Exception ex)
        {
            HelperLog.Info($"Riavvio sessione continua fallito: {ex}");
            var wrap = new InvalidOperationException("La sessione di riconoscimento si è fermata e non riparte.", ex);
            if (_phase == ListenPhase.WakeOpen)
                _wakeTcs?.TrySetException(wrap);
            else
                _dictationTcs?.TrySetException(wrap);
        }
    }

    private async Task TearDownRecognizerAsync()
    {
        var recognizer = _recognizer;
        if (recognizer is null)
            return;

        _stopping = true;
        try
        {
            recognizer.HypothesisGenerated -= OnHypothesisGenerated;
            recognizer.ContinuousRecognitionSession.ResultGenerated -= OnResultGenerated;
            recognizer.ContinuousRecognitionSession.Completed -= OnSessionCompleted;
        }
        catch (Exception ex)
        {
            HelperLog.Info($"Unsubscribe eventi: {ex.Message}");
        }

        try
        {
            // StopAsync scarica i result pendenti; se è già Idle, WinRT può lanciare: ignora.
            await recognizer.ContinuousRecognitionSession.StopAsync();
        }
        catch (Exception)
        {
            try
            {
                await recognizer.ContinuousRecognitionSession.CancelAsync();
            }
            catch (Exception)
            {
                // Dispose sotto comunque: meglio un riconoscitore morto che un mic bloccato.
            }
        }

        try
        {
            recognizer.Dispose();
        }
        catch (Exception ex)
        {
            HelperLog.Info($"Dispose SpeechRecognizer: {ex.Message}");
        }

        _recognizer = null;
        _stopping = false;
        _hypothesis = "";
    }

    private static string Combine(string left, string right)
    {
        // Uno dei due è spesso vuoto (solo hypotesis, o solo committed): niente doppio spazio.
        left = left.Trim();
        right = right.Trim();
        if (left.Length == 0)
            return right;
        if (right.Length == 0)
            return left;
        return left + " " + right;
    }

    private enum ListenPhase
    {
        Idle,
        WakeOpen,
        Dictation,
    }

    private enum RecognizerKind
    {
        WakeOpen,
        Dictation,
    }

    private enum DictationKind
    {
        Close,
        Cancel,
        TimeoutWithText,
        TimeoutEmpty,
    }

    private readonly struct DictationOutcome
    {
        public DictationKind Kind { get; }
        public string Transcript { get; }

        private DictationOutcome(DictationKind kind, string transcript)
        {
            Kind = kind;
            Transcript = transcript;
        }

        public static DictationOutcome Close(string transcript) => new(DictationKind.Close, transcript);

        public static DictationOutcome Cancel() => new(DictationKind.Cancel, "");

        public static DictationOutcome TimeoutWithText(string transcript) =>
            new(DictationKind.TimeoutWithText, transcript);

        public static DictationOutcome TimeoutEmpty() => new(DictationKind.TimeoutEmpty, "");

        public static DictationOutcome Canceled() => new(DictationKind.Cancel, "");
    }
}

/// <summary>
/// Match e strip delle wake sul testo WinRT. Normalizziamo minuscole, punteggiatura
/// → spazio, spazi compressi: «Assistente, chiudi.» deve valere come «assistente chiudi».
/// Confine di parola: «assistente chiudiamo» NON deve matchare «assistente chiudi».
/// </summary>
internal static class PhraseMatcher
{
    /// <summary>
    /// «ehi assistente» → «hei assistente»: stessa sveglia, WinRT it-IT spesso sente la H.
    /// </summary>
    public static string? HeiAlias(string wakeOpen)
    {
        var n = Normalize(wakeOpen);
        if (!n.StartsWith("ehi ", StringComparison.Ordinal))
            return null;
        return "hei " + n["ehi ".Length..];
    }
    public static bool ContainsPhrase(string haystack, string phrase)
    {
        var h = Normalize(haystack);
        var p = Normalize(phrase);
        if (p.Length == 0 || h.Length == 0)
            return false;
        // Confine di token già normalizzati (spazi singoli): evita il prefisso «chiudi»/«chiudiamo».
        var pattern = @"(^|\s)" + Regex.Escape(p) + @"($|\s)";
        return Regex.IsMatch(h, pattern, RegexOptions.CultureInvariant);
    }

    /// <summary>
    /// Toglie l'ultima occorrenza della frase (la wake di chiusura in coda).
    /// Opera sul testo già visibile a Gemini, non solo sulla forma normalizzata,
    /// così restano le virgole della dettatura.
    /// </summary>
    public static string StripPhrase(string raw, string phrase)
    {
        var words = Normalize(phrase).Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (words.Length == 0)
            return raw.Trim();

        // Tra una parola e l'altra accettiamo spazi e punteggiatura WinRT («chiudi.»).
        var pattern = string.Join(@"[\s\p{P}]+", words.Select(Regex.Escape));
        var rx = new Regex(pattern, RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        Match? last = null;
        foreach (Match match in rx.Matches(raw))
            last = match;
        if (last is null || !last.Success)
            return NormalizeSpacing(raw);

        var cut = raw.Remove(last.Index, last.Length);
        return NormalizeSpacing(cut);
    }

    public static string Normalize(string text)
    {
        if (string.IsNullOrWhiteSpace(text))
            return "";

        var sb = new StringBuilder(text.Length);
        foreach (var ch in text)
        {
            if (char.IsLetterOrDigit(ch))
            {
                sb.Append(char.ToLowerInvariant(ch));
            }
            else
            {
                // Punteggiatura e apostrofi diventano spazio: «ehi, assistente» → due token.
                sb.Append(' ');
            }
        }

        return NormalizeSpacing(sb.ToString());
    }

    private static string NormalizeSpacing(string text)
    {
        return string.Join(
            " ",
            text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
    }
}
