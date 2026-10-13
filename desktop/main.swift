import AppKit
import Foundation

// A native Dock application owns one Python service and handles Dock reopen
// events even after its browser window has been closed.
final class ConvoyLauncher: NSObject, NSApplicationDelegate {
    private var child: Process?
    private var output: Pipe?
    private var browserURL: URL?
    private var buffer = Data()
    private var generation = 0
    private var quitting = false
    private var starting = false
    private let verifyOnly: Bool
    private let project: URL

    init(project: URL, verifyOnly: Bool) {
        self.project = project
        self.verifyOnly = verifyOnly
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let actions = NSMenu()
        actions.addItem(withTitle: "打开历史中心", action: #selector(reopen), keyEquivalent: "o").target = self
        actions.addItem(.separator())
        actions.addItem(withTitle: "退出 Agent Convoy", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = actions
        NSApp.mainMenu = menu
        start()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        reopen()
        return true
    }

    @objc func reopen() {
        if let process = child, process.isRunning, let url = browserURL {
            openBrowser(url)
        } else if !starting {
            start()
        }
    }

    private func start() {
        generation += 1
        let current = generation
        starting = true
        browserURL = nil
        buffer.removeAll()
        let script = project.appendingPathComponent("agent_convoy.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            fail("找不到 Agent Convoy 程序。请把应用放回 agent-convoy 项目目录。")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-u", script.path, "ui", "--no-open"]
        process.currentDirectoryURL = project
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        // Finder/Dock does not supply the login-shell PATH.
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
        process.environment = env
        let pipe = Pipe()
        process.standardOutput = pipe
        if verifyOnly {
            process.standardError = FileHandle.standardError
        } else {
            let logs = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Logs/AgentConvoy", isDirectory: true)
            do {
                try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700])
                let file = logs.appendingPathComponent("launcher.log")
                if !FileManager.default.fileExists(atPath: file.path) {
                    FileManager.default.createFile(atPath: file.path, contents: nil,
                        attributes: [.posixPermissions: 0o600])
                }
                let handle = try FileHandle(forWritingTo: file)
                handle.seekToEndOfFile()
                process.standardError = handle
            } catch {
                fail("无法创建启动日志：\(error.localizedDescription)")
                return
            }
        }
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                guard let self = self, self.generation == current else { return }
                self.received(data)
            }
        }
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self = self, !self.quitting, self.generation == current else { return }
                self.starting = false
                self.browserURL = nil
                self.fail("本机服务已退出（状态 \(finished.terminationStatus)）。启动日志位于 ~/Library/Logs/AgentConvoy/launcher.log。")
            }
        }
        child = process
        output = pipe
        do { try process.run() }
        catch { fail("无法启动 Python 服务：\(error.localizedDescription)"); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            guard let self = self, self.generation == current, self.starting else { return }
            self.fail("本机服务启动超时。请查看 ~/Library/Logs/AgentConvoy/launcher.log。")
        }
    }

    private func received(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = String(data: buffer[..<newline], encoding: .utf8) ?? ""
            buffer.removeSubrange(...newline)
            let prefix = "Open this one-time local URL: "
            guard line.hasPrefix(prefix), let url = URL(string: String(line.dropFirst(prefix.count))),
                  url.scheme == "http", url.host == "127.0.0.1", url.port != nil else { continue }
            browserURL = url
            checkService(url)
        }
    }

    private func checkService(_ url: URL) {
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        let token = components.queryItems?.first(where: { $0.name == "token" })?.value ?? ""
        components.path = "/api/ledger"
        components.query = nil
        var request = URLRequest(url: components.url!)
        request.setValue(token, forHTTPHeaderField: "X-Agent-Convoy-Token")
        request.timeoutInterval = 10
        let current = generation
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            DispatchQueue.main.async {
                guard let self = self, self.generation == current, !self.quitting else { return }
                guard error == nil, (response as? HTTPURLResponse)?.statusCode == 200 else {
                    self.fail("本机服务未能响应：\(error?.localizedDescription ?? "无效 HTTP 响应")")
                    return
                }
                self.starting = false
                if self.verifyOnly {
                    print("Native launcher OK: authenticated loopback API returned HTTP 200; browser not opened.")
                    NSApp.terminate(nil)
                } else { self.openBrowser(url) }
            }
        }.resume()
    }

    private func openBrowser(_ url: URL) {
        guard NSWorkspace.shared.open(url) else {
            fail("无法打开默认浏览器，请检查 macOS 默认浏览器设置。")
            return
        }
    }

    private func fail(_ reason: String) {
        starting = false
        if verifyOnly {
            FileHandle.standardError.write(Data((reason + "\n").utf8))
            stop()
            exit(1)
        }
        let alert = NSAlert()
        alert.messageText = "Agent Convoy 启动失败"
        alert.informativeText = reason
        alert.addButton(withTitle: "关闭")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        NSApp.terminate(nil)
    }

    private func stop() {
        quitting = true
        output?.fileHandleForReading.readabilityHandler = nil
        if let process = child, process.isRunning {
            process.interrupt()
            process.waitUntilExit()
        }
    }

    func applicationWillTerminate(_ notification: Notification) { stop() }
}

let verifyOnly = CommandLine.arguments.contains("--verify-launch")
let project: URL
if verifyOnly, let i = CommandLine.arguments.firstIndex(of: "--project"), i + 1 < CommandLine.arguments.count {
    project = URL(fileURLWithPath: CommandLine.arguments[i + 1], isDirectory: true)
} else {
    project = Bundle.main.bundleURL.deletingLastPathComponent()
}
let application = NSApplication.shared
application.setActivationPolicy(verifyOnly ? .prohibited : .regular)
let delegate = ConvoyLauncher(project: project, verifyOnly: verifyOnly)
application.delegate = delegate
application.run()
