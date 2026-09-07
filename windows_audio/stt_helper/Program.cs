using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace SttHelper;

/// <summary>
/// Entry point dell'helper STT Windows.
///
/// Contratto verso il server Python sullo stesso host (non verso WSL):
/// ascolta solo <c>127.0.0.1:8766</c>. WSL parla con Python su :8765; Python
/// inoltra <c>POST /listen</c> qui e aspetta <c>{"transcript":"..."}</c>.
///
/// Perché WinForms e non un thread nudo: WinRT <c>SpeechRecognizer</c> è COM/WinRT
/// in STA. Gli eventi (hypotesis, ResultGenerated) arrivano nella coda messaggi
/// di quel thread. <see cref="Application.Run"/> pompa i messaggi; un
/// <c>Thread.Sleep</c> in STA senza pump non consegna i risultati.
///
/// Side-effect: process persistente, form invisibile, log in %TEMP%\stt_helper.log.
/// Non tocca il loop vocale WSL. Avvio tipico: <c>dotnet run</c> da Windows,
/// oppure spawn dal Python host.
/// </summary>
internal static class Program
{
    /// <summary>
    /// STA obbligatorio: senza <see cref="STAThreadAttribute"/> WinForms crea
    /// comunque uno STA, ma lo dichiaramo esplicito così un refactor a
    /// <c>async Task Main</c> (che è MTA) non passa inosservato.
    /// </summary>
    [STAThread]
    private static void Main(string[] args)
    {
        // DPI / visual styles prima di qualsiasi HWND. Evitiamo ApplicationConfiguration
        // (source generator) così il csproj resta un SDK “nudo” senza file extra.
        Application.SetHighDpiMode(HighDpiMode.SystemAware);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        // Argomenti vincono sull'env, env vince sui default «ehi assistente» / chiudi / annulla.
        var settings = HelperSettings.FromArgsAndEnv(args);
        HelperLog.Info(
            $"Avvio stt_helper porta={settings.Port} " +
            $"open='{settings.WakeOpen}' close='{settings.WakeClose}' cancel='{settings.WakeCancel}'");

        // La form esiste solo per HWND + SynchronizationContext. Mai visibile.
        var form = new HiddenStaForm();
        var loop = new SpeechListenLoop(form, settings);
        var server = new LoopbackListenServer(settings.Port, loop);

        // HWND prima di HTTP: BeginInvoke dal thread pool (PostToSta) lo richiede.
        // Non usare Form.Load: SetVisibleCore(false) non mostra mai la finestra,
        // Load non parte, /health resta chiuso e Python scade a 90s.
        // CreateHandle() è protected: la form espone EnsureHandle() pubblico.
        form.EnsureHandle();

        try
        {
            // HttpListener sta sul pool, non sullo STA. WinRT arriva solo su /listen,
            // quando Application.Run sta già pompando.
            server.Start();
            HelperLog.Info("HttpListener in ascolto.");
        }
        catch (Exception ex)
        {
            // Porta occupata o urlacl: l'utente deve vederlo, non solo il log.
            HelperLog.Info($"Avvio HTTP fallito: {ex}");
            MessageBox.Show(
                $"stt_helper non riesce ad ascoltare su 127.0.0.1:{settings.Port}. {ex.Message}",
                "stt_helper",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return;
        }

        // Chiusura processo: smetti di accettare socket e ferma WinRT sullo STA.
        form.FormClosed += (_, _) =>
        {
            server.Stop();
            loop.Shutdown();
        };

        Application.Run(form);
    }
}

/// <summary>
/// Frasi wake e porta loopback. Default da tre parole così in dettatura
/// «chiudi» / «annulla» da soli non chiudono il turno (una mail può dirlo).
/// </summary>
internal sealed class HelperSettings
{
    public const int DefaultPort = 8766;
    public const string DefaultWakeOpen = "ehi assistente";
    public const string DefaultWakeClose = "assistente chiudi";
    public const string DefaultWakeCancel = "assistente annulla";
    // Tetto dettatura dopo la wake di apertura: chiusura implicita se c'è testo.
    public const int DictationTimeoutSeconds = 45;

    public string WakeOpen { get; init; } = DefaultWakeOpen;
    public string WakeClose { get; init; } = DefaultWakeClose;
    public string WakeCancel { get; init; } = DefaultWakeCancel;
    // 127.0.0.1 only: il Python host è l'unico client previsto.
    public int Port { get; init; } = DefaultPort;

    /// <summary>
    /// Precedenza: default → env (<c>WAKE_OPEN</c> / <c>WAKE_CLOSE</c> /
    /// <c>WAKE_CANCEL</c> / <c>STT_HELPER_PORT</c>) → argomenti
    /// <c>--wake-open</c> ecc. Stringhe vuote tornano al default: una wake
    /// vuota farebbe matchare ogni hypotesis.
    /// </summary>
    public static HelperSettings FromArgsAndEnv(string[] args)
    {
        var wakeOpen = ReadNonEmptyEnv("WAKE_OPEN") ?? DefaultWakeOpen;
        var wakeClose = ReadNonEmptyEnv("WAKE_CLOSE") ?? DefaultWakeClose;
        var wakeCancel = ReadNonEmptyEnv("WAKE_CANCEL") ?? DefaultWakeCancel;
        var port = DefaultPort;
        // Porta override: utile se 8766 è già presa da un helper zombie.
        var portEnv = ReadNonEmptyEnv("STT_HELPER_PORT");
        if (portEnv is not null && int.TryParse(portEnv, out var parsedEnv) && parsedEnv is > 0 and < 65536)
            port = parsedEnv;

        for (var i = 0; i < args.Length; i++)
        {
            var token = args[i];
            if (TryReadOption(token, args, ref i, "--wake-open", out var openArg) && openArg.Length > 0)
                wakeOpen = openArg;
            else if (TryReadOption(token, args, ref i, "--wake-close", out var closeArg) && closeArg.Length > 0)
                wakeClose = closeArg;
            else if (TryReadOption(token, args, ref i, "--wake-cancel", out var cancelArg) && cancelArg.Length > 0)
                wakeCancel = cancelArg;
            else if (TryReadOption(token, args, ref i, "--port", out var portArg)
                     && int.TryParse(portArg, out var parsedArg)
                     && parsedArg is > 0 and < 65536)
                port = parsedArg;
        }

        return new HelperSettings
        {
            WakeOpen = wakeOpen.Trim(),
            WakeClose = wakeClose.Trim(),
            WakeCancel = wakeCancel.Trim(),
            Port = port,
        };
    }

    private static string? ReadNonEmptyEnv(string name)
    {
        var value = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
    }

    /// <summary>
    /// Accetta <c>--key value</c> e <c>--key=value</c>. Incrementa <paramref name="index"/>
    /// solo nella forma a due token, così il for del chiamante non salta l'argomento dopo.
    /// </summary>
    private static bool TryReadOption(
        string token,
        string[] args,
        ref int index,
        string key,
        out string value)
    {
        value = "";
        if (token.Equals(key, StringComparison.OrdinalIgnoreCase))
        {
            if (index + 1 >= args.Length)
                return false;
            index++;
            value = args[index];
            return true;
        }

        var prefix = key + "=";
        if (token.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            value = token[prefix.Length..];
            return true;
        }

        return false;
    }
}

/// <summary>
/// Form 1×1 mai visibile. HWND + sync context WinForms = pump STA per WinRT.
/// BeginInvoke dal thread pool HTTP marshala creazione/stop del riconoscitore.
/// </summary>
internal sealed class HiddenStaForm : Form
{
    public HiddenStaForm()
    {
        Text = "stt_helper";
        ShowInTaskbar = false;
        FormBorderStyle = FormBorderStyle.FixedToolWindow;
        StartPosition = FormStartPosition.Manual;
        Location = new Point(-32000, -32000);
        Size = new Size(1, 1);
        Opacity = 0;
        WindowState = FormWindowState.Minimized;
    }

    protected override CreateParams CreateParams
    {
        get
        {
            const int WsExToolwindow = 0x00000080;
            var cp = base.CreateParams;
            // Toolwindow: fuori da Alt+Tab. Non è sicurezza, è solo rumore UI.
            cp.ExStyle |= WsExToolwindow;
            return cp;
        }
    }

    /// <summary>
    /// Crea l'HWND senza mostrare la finestra. Pubblico perché <c>Main</c> deve
    /// avere l'handle prima di <c>Application.Run</c> (HTTP parte prima del pump).
    /// <c>Control.CreateHandle()</c> è <c>protected</c>: questo wrapper lo espone.
    /// </summary>
    public void EnsureHandle()
    {
        if (!IsHandleCreated)
            CreateHandle();
    }

    protected override void SetVisibleCore(bool value)
    {
        // Application.Run chiama Show(); noi creiamo l'handle e restiamo invisibili.
        // Sempre false: Load/Shown non partono (è voluto). HTTP parte da Main, non da Load.
        EnsureHandle();
        base.SetVisibleCore(false);
    }

    /// <summary>
    /// Posta <paramref name="action"/> sulla coda STA. Mai Invoke sincrono dal
    /// thread HTTP: se l'azione aspetta un evento WinRT, il pump è bloccato e
    /// si deadlocka. BeginInvoke torna subito; l'HTTP attende un TaskCompletionSource.
    /// </summary>
    public void PostToSta(Action action)
    {
        if (IsDisposed)
            return;
        try
        {
            if (!IsHandleCreated)
                CreateHandle();
            BeginInvoke(action);
        }
        catch (ObjectDisposedException)
        {
            // Processo in uscita: ignorare è corretto, non c'è più pump.
        }
        catch (InvalidOperationException)
        {
            // Handle distrutto tra il check e BeginInvoke.
        }
    }
}

/// <summary>
/// HttpListener loopback. Thread pool: niente WinRT qui. Un solo <c>/listen</c>
/// in volo; un secondo preempta il primo (WSL ha timeout 300s e può riprovare
/// mentre questa connessione è ancora aperta).
/// </summary>
internal sealed class LoopbackListenServer
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        // Transcript italiano leggibile nei log; Python json.loads accetta UTF-8.
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private readonly int _port;
    private readonly SpeechListenLoop _loop;
    private readonly HttpListener _listener = new();
    private readonly object _listenGate = new();
    private CancellationTokenSource? _currentListenCts;
    private Task? _currentListenTask;
    private CancellationTokenSource? _acceptCts;
    private Task? _acceptTask;

    public LoopbackListenServer(int port, SpeechListenLoop loop)
    {
        _port = port;
        _loop = loop;
        // Solo localhost: WSL non deve raggiungere 8766 (NAT vede solo Python :8765).
        _listener.Prefixes.Add($"http://127.0.0.1:{_port}/");
        // Client WSL/Python staccato a metà listen: non far esplodere Write.
        _listener.IgnoreWriteExceptions = true;
    }

    public void Start()
    {
        _listener.Start();
        _acceptCts = new CancellationTokenSource();
        var token = _acceptCts.Token;
        // Accept su thread pool: GetContextAsync non deve stare sullo STA.
        _acceptTask = Task.Run(() => AcceptLoopAsync(token), token);
    }

    public void Stop()
    {
        try
        {
            _acceptCts?.Cancel();
            // Abort sblocca GetContextAsync; Stop da solo a volte resta appeso.
            if (_listener.IsListening)
                _listener.Abort();
        }
        catch (ObjectDisposedException)
        {
            // Già chiuso: shutdown idempotente.
        }

        lock (_listenGate)
        {
            _currentListenCts?.Cancel();
        }
    }

    private async Task AcceptLoopAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            HttpListenerContext context;
            try
            {
                context = await _listener.GetContextAsync().ConfigureAwait(false);
            }
            catch (ObjectDisposedException)
            {
                break;
            }
            catch (HttpListenerException)
            {
                // Abort/Stop: usciamo senza rumore se stiamo chiudendo.
                if (token.IsCancellationRequested || !_listener.IsListening)
                    break;
                HelperLog.Info("GetContextAsync ha lanciato HttpListenerException; riprovo.");
                continue;
            }

            // Ogni request sul pool: /health non deve aspettare /listen.
            _ = Task.Run(() => HandleRequestAsync(context), token);
        }
    }

    private async Task HandleRequestAsync(HttpListenerContext context)
    {
        var path = context.Request.Url?.AbsolutePath?.TrimEnd('/') ?? "";
        if (path.Length == 0)
            path = "/";
        var method = context.Request.HttpMethod ?? "";

        try
        {
            if (method == "GET" && (path == "/health" || path == "/"))
            {
                // Il Python host può fare polling finché l'helper non è pronto.
                WriteJson(context, 200, """{"ok":true}""");
                return;
            }

            if (method == "POST" && path == "/listen")
            {
                await HandleListenAsync(context).ConfigureAwait(false);
                return;
            }

            WriteStatus(context, 404);
        }
        catch (Exception ex)
        {
            HelperLog.Info($"HandleRequest: {ex}");
            try
            {
                WriteStatus(context, 500);
            }
            catch
            {
                // Seconda failure in write: niente da fare, il client è già andato.
            }
        }
    }

    private async Task HandleListenAsync(HttpListenerContext context)
    {
        var cts = new CancellationTokenSource();
        Task? previousTask;
        CancellationTokenSource? previousCts;

        lock (_listenGate)
        {
            previousCts = _currentListenCts;
            previousTask = _currentListenTask;
            _currentListenCts = cts;
        }

        // Preempt: il client precedente ha probabilmente toccato il timeout WSL 300s.
        if (previousCts is not null)
        {
            try
            {
                previousCts.Cancel();
            }
            catch (ObjectDisposedException)
            {
                // Già disposed dal handler precedente: ok.
            }

            if (previousTask is not null)
            {
                try
                {
                    await previousTask.ConfigureAwait(false);
                }
                catch (Exception)
                {
                    // Canceled / faulted: il handler vecchio scrive 503 da solo.
                }
            }

            previousCts.Dispose();
        }

        var listenTask = _loop.ListenAsync(cts.Token);
        lock (_listenGate)
        {
            _currentListenTask = listenTask;
        }

        try
        {
            await listenTask.ConfigureAwait(false);
            WriteJson(context, 200, """{"awake":true}""");
        }
        catch (OperationCanceledException)
        {
            // Preempt o shutdown: niente transcript vuoto (quel JSON farebbe uscire il loop WSL).
            WriteStatus(context, 503);
        }
        catch (Exception ex)
        {
            HelperLog.Info($"ListenAsync fallito: {ex}");
            var json = JsonSerializer.Serialize(
                new ErrorResponse(ex.Message),
                JsonOptions);
            WriteJson(context, 500, json);
        }
        finally
        {
            lock (_listenGate)
            {
                if (ReferenceEquals(_currentListenCts, cts))
                    _currentListenCts = null;
            }

            cts.Dispose();
        }
    }

    private static void WriteJson(HttpListenerContext context, int status, string json)
    {
        var bytes = Encoding.UTF8.GetBytes(json);
        var response = context.Response;
        response.StatusCode = status;
        response.ContentType = "application/json; charset=utf-8";
        response.ContentLength64 = bytes.Length;
        response.OutputStream.Write(bytes, 0, bytes.Length);
        response.OutputStream.Close();
        response.Close();
    }

    private static void WriteStatus(HttpListenerContext context, int status)
    {
        var response = context.Response;
        response.StatusCode = status;
        response.ContentLength64 = 0;
        response.Close();
    }

    private sealed record ErrorResponse(string error);
}

/// <summary>
/// Log di studio: WinExe non ha console. %TEMP%\stt_helper.log + OutputDebugString.
/// Non è telemetria prodotto; in produzione si potrebbe spegnere.
/// </summary>
internal static class HelperLog
{
    private static readonly string LogPath = Path.Combine(Path.GetTempPath(), "stt_helper.log");

    public static void Info(string message)
    {
        var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {message}";
        Debug.WriteLine(line);
        try
        {
            File.AppendAllText(LogPath, line + Environment.NewLine);
        }
        catch
        {
            // Disco pieno / permessi TEMP: il riconoscitore deve comunque vivere.
        }
    }
}
