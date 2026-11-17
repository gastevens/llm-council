// War Room.app executable. Starts macos/launcher.sh (a dedicated Ghostty window
// running the TUI) and quits when every window it started has closed.
//
// A folder or file dropped on the app icon (or `open -a "War Room" ~/folder`)
// becomes the session's context folder: the TUI is started with --context PATH.
// Extra arguments (`open -a "War Room" --args --fake`) are passed through.
import Cocoa

final class Delegate: NSObject, NSApplicationDelegate {
    private var pending: [String] = []
    private var started = false
    private var launched = 0
    private var running = 0
    private let launcher = (Bundle.main.resourcePath ?? "") + "/launcher.sh"
    private lazy var cliArgs: [String] = {
        Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
    }()

    func application(_ sender: NSApplication, open urls: [URL]) {
        let paths = urls.filter { $0.isFileURL }.map { $0.path }
        if started {
            paths.forEach { launchSession(context: $0) }  // dropped while running: new window
        } else {
            pending += paths
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Give Launch Services a moment to deliver any dropped folders first.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            self.started = true
            let fromArgs = Set(self.cliArgs.map { URL(fileURLWithPath: $0).standardizedFileURL.path })
            let dropped = self.pending.filter { !fromArgs.contains(URL(fileURLWithPath: $0).standardizedFileURL.path) }
            self.launchSession(context: dropped.first)
        }
    }

    private func launchSession(context: String?) {
        var args = launched == 0 ? cliArgs : []
        if let c = context { args += ["--context", c] }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launcher)
        p.arguments = args
        p.terminationHandler = { _ in
            DispatchQueue.main.async {
                self.running -= 1
                if self.running <= 0 { NSApp.terminate(nil) }
            }
        }
        do {
            try p.run()
            launched += 1
            running += 1
        } catch {
            if running <= 0 { NSApp.terminate(nil) }
        }
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
