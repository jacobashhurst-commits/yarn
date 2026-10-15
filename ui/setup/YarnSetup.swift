// Yarn Setup - a window, not a terminal.
//
//   YarnSetup --repo /path/to/yarn. --python /usr/bin/python3
//
// Built by bin/yarn-setup into an .app and launched with `open`.

import AppKit
import Darwin

struct Step: Equatable {
    let id: String
    let title: String
    var state: String = "pending" // pending | running | done | failed
    var elapsed: String = ""
    var detail: String = ""
}

func fmtMs(_ ms: Int) -> String {
    if ms < 1000 { return "\(ms)ms" }
    let s = Double(ms) / 1000.0
    if s < 60 { return String(format: "%.1fs", s) }
    return String(format: "%.0fm %.0fs", s / 60, s.truncatingRemainder(dividingBy: 60))
}

final class Controller: NSObject, NSWindowDelegate {
    let repo: String
    let python: String
    var steps: [Step] = []
    var worker: Process?
    var finished = false

    let window: NSWindow
    let heading = NSTextField(labelWithString: "Welcome to Yarn")
    let subtitle = NSTextField(labelWithString: "")
    let welcome = NSTextField(wrappingLabelWithString: "")
    let startButton = NSButton(title: "Get started", target: nil, action: nil)
    let fastBox = NSButton(checkboxWithTitle: "Fast summaries (smaller, quicker)", target: nil, action: nil)
    let accurateBox = NSButton(checkboxWithTitle: "Accurate summaries (better writing, more memory)", target: nil, action: nil)
    let summaryHint = NSTextField(wrappingLabelWithString: "")
    let bar = NSProgressIndicator()
    let list = NSStackView()
    let detail = NSTextField(wrappingLabelWithString: "")
    let cancel = NSButton(title: "Not now", target: nil, action: nil)
    var statusLine = ""
    var statusNote = ""
    var stepIndex = 1
    var stepTotal = 1
    var rowLabels: [NSTextField] = []
    var rowTimes: [NSTextField] = []
    var rowMarks: [NSTextField] = []

    init(repo: String, python: String) {
        self.repo = repo
        self.python = python
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 640),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "Yarn"
        window.delegate = self
        window.isReleasedWhenClosed = false
        build()
    }

    func start() {
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func build() {
        heading.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        subtitle.font = NSFont.systemFont(ofSize: 13)
        subtitle.textColor = .secondaryLabelColor
        welcome.font = NSFont.systemFont(ofSize: 14)
        welcome.textColor = .labelColor
        welcome.maximumNumberOfLines = 0
        welcome.preferredMaxLayoutWidth = 664
        welcome.stringValue = """
        Yarn records meetings you start, on this Mac only. Nothing is uploaded.

        When this finishes, Yarn is in the Dock. Click it to record, watch a meeting process, name the voices, read the daily brief, or ask about your meetings. A small menu-bar icon does the same things.

        It will install two free, open-source apps if they are not already here:

        • SwiftBar, the menu-bar icon
        • Ollama, summaries and Ask Yarn, still on this computer

        macOS may ask for your password or microphone permission. That’s normal. Click Get started and wait. The long steps are downloads, not homework.
        """
        let ram = memoryGB()
        let neo = ram > 0 && ram <= 10
        fastBox.state = .on
        accurateBox.state = neo ? .off : .on
        accurateBox.isEnabled = !neo
        summaryHint.font = NSFont.systemFont(ofSize: 12)
        summaryHint.textColor = .secondaryLabelColor
        summaryHint.maximumNumberOfLines = 0
        summaryHint.preferredMaxLayoutWidth = 664
        if neo {
            summaryHint.stringValue = String(
                format: "This Mac has %.0f GB, so it can only run Fast. Accurate needs about 16 GB.",
                ram
            )
            fastBox.isEnabled = false
        } else {
            summaryHint.stringValue = "Download Fast, Accurate, or both. You pick which one Yarn uses in the menu bar."
        }
        startButton.target = self
        startButton.action = #selector(beginInstall)
        startButton.keyEquivalent = "\r"
        bar.isHidden = true
        list.isHidden = true
        detail.isHidden = true
        bar.isIndeterminate = false
        bar.minValue = 0
        bar.maxValue = 100
        bar.doubleValue = 0
        detail.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        detail.textColor = .secondaryLabelColor
        detail.maximumNumberOfLines = 6
        cancel.target = self
        cancel.action = #selector(cancelRun)
        cancel.keyEquivalent = "\u{1b}"

        list.orientation = .vertical
        list.alignment = .leading
        list.spacing = 10
        list.translatesAutoresizingMaskIntoConstraints = false

        let stack = NSStackView(views: [
            heading, subtitle, welcome, summaryHint, fastBox, accurateBox,
            startButton, bar, list, detail, cancel,
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 28, bottom: 20, right: 28)
        stack.translatesAutoresizingMaskIntoConstraints = false
        welcome.widthAnchor.constraint(equalToConstant: 664).isActive = true
        bar.widthAnchor.constraint(equalToConstant: 664).isActive = true
        detail.widthAnchor.constraint(equalToConstant: 664).isActive = true

        window.contentView = NSView(frame: NSRect(x: 0, y: 0, width: 720, height: 640))
        window.contentView?.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: window.contentView!.bottomAnchor),
        ])
    }

    private func rebuildList() {
        list.arrangedSubviews.forEach { $0.removeFromSuperview() }
        rowLabels = []
        rowTimes = []
        rowMarks = []
        for step in steps {
            let mark = NSTextField(labelWithString: glyph(step.state))
            mark.font = NSFont.systemFont(ofSize: 13)
            mark.textColor = color(step.state)
            mark.widthAnchor.constraint(equalToConstant: 22).isActive = true
            let title = NSTextField(labelWithString: step.title)
            title.font = NSFont.systemFont(
                ofSize: 14,
                weight: step.state == "running" ? .medium : .regular
            )
            title.textColor = step.state == "pending" ? .tertiaryLabelColor : .labelColor
            let time = NSTextField(labelWithString: step.elapsed)
            time.font = NSFont.systemFont(ofSize: 12)
            time.textColor = .tertiaryLabelColor
            time.alignment = .right
            time.widthAnchor.constraint(equalToConstant: 72).isActive = true
            let row = NSStackView(views: [mark, title, NSView(), time])
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = 8
            row.translatesAutoresizingMaskIntoConstraints = false
            row.widthAnchor.constraint(equalToConstant: 664).isActive = true
            list.addArrangedSubview(row)
            rowMarks.append(mark)
            rowLabels.append(title)
            rowTimes.append(time)
        }
    }

    private func refreshRows() {
        guard rowLabels.count == steps.count else {
            rebuildList()
            return
        }
        for i in steps.indices {
            rowMarks[i].stringValue = glyph(steps[i].state)
            rowMarks[i].textColor = color(steps[i].state)
            rowLabels[i].stringValue = steps[i].title
            rowLabels[i].textColor = steps[i].state == "pending" ? .tertiaryLabelColor : .labelColor
            rowLabels[i].font = NSFont.systemFont(
                ofSize: 14,
                weight: steps[i].state == "running" ? .medium : .regular
            )
            rowTimes[i].stringValue = steps[i].elapsed
        }
    }

    private func glyph(_ state: String) -> String {
        switch state {
        case "done": return "✓"
        case "failed": return "✕"
        case "running": return "●"
        default: return "○"
        }
    }

    private func color(_ state: String) -> NSColor {
        switch state {
        case "done": return NSColor.systemGreen
        case "failed": return NSColor.systemRed
        case "running": return NSColor.controlAccentColor
        default: return NSColor.tertiaryLabelColor
        }
    }

    private func launchWorker() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        var pull: [String] = []
        if fastBox.state == .on { pull.append("fast") }
        if accurateBox.state == .on { pull.append("accurate") }
        if pull.isEmpty { pull = ["fast"] }
        process.arguments = [
            "-m", "meeting_archive.setup",
            "--repo", repo,
            "--pull", pull.joined(separator: ","),
        ]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = repo
        env["PATH"] = "\(repo)/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        process.environment = env
        process.currentDirectoryURL = URL(fileURLWithPath: repo)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { return }
            let text = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async { self?.ingest(text) }
        }
        process.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                self?.workerFinished(code: proc.terminationStatus)
            }
        }
        do {
            try process.run()
            worker = process
        } catch {
            subtitle.stringValue = "Could not start setup"
            detail.stringValue = "\(error)"
        }
    }

    private var buffer = ""

    private func ingest(_ chunk: String) {
        buffer += chunk
        while let range = buffer.range(of: "\n") {
            let line = String(buffer[..<range.lowerBound])
            buffer = String(buffer[range.upperBound...])
            handle(line)
        }
    }

    private func handle(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("{"),
              let data = trimmed.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = obj["type"] as? String
        else {
            if !trimmed.isEmpty { detail.stringValue = trimmed }
            return
        }
        switch type {
        case "hello":
            if let raw = obj["steps"] as? [[String: Any]] {
                steps = raw.compactMap { item in
                    guard let id = item["id"] as? String,
                          let title = item["title"] as? String else { return nil }
                    return Step(id: id, title: title)
                }
                rebuildList()
                subtitle.stringValue = "1 of \(steps.count) steps"
            }
        case "start":
            let id = obj["id"] as? String ?? ""
            let index = obj["index"] as? Int ?? 1
            let total = obj["total"] as? Int ?? steps.count
            set(id, state: "running")
            heading.stringValue = obj["title"] as? String ?? "Install Yarn"
            stepIndex = index
            stepTotal = total
            statusNote = ""
            subtitle.stringValue = "\(index) of \(total) steps"
            bar.doubleValue = Double(index - 1) * 100.0 / Double(max(total, 1))
            cancel.isEnabled = true
        case "log":
            if let text = obj["text"] as? String {
                statusLine = text
                statusNote = obj["note"] as? String ?? ""
                showStatus(percent: nil)
            }
        case "progress":
            if let percent = obj["percent"] as? Int {
                let shown = min(100, max(0, percent))
                bar.doubleValue = Double(shown)
                subtitle.stringValue = "\(stepIndex) of \(stepTotal) steps, \(shown)%"
                showStatus(percent: shown)
            }
        case "done":
            let id = obj["id"] as? String ?? ""
            let ok = obj["ok"] as? Bool ?? false
            let ms = obj["elapsed_ms"] as? Int ?? 0
            set(id, state: ok ? "done" : "failed", elapsed: fmtMs(ms))
            if !ok, let message = obj["message"] as? String {
                detail.stringValue = message
            }
        case "finish":
            finished = true
            let ok = obj["ok"] as? Bool ?? false
            heading.stringValue = ok ? "Yarn is ready" : "Setup stopped"
            subtitle.stringValue = ok ? "Finished" : "Something needs attention"
            bar.doubleValue = ok ? 100 : bar.doubleValue
            if let message = obj["message"] as? String {
                detail.stringValue = message
            }
            cancel.title = "Close"
            cancel.isEnabled = true
        default:
            break
        }
        refreshRows()
    }

    private func showStatus(percent: Int?) {
        var lines: [String] = []
        if !statusLine.isEmpty { lines.append(statusLine) }
        if !statusNote.isEmpty { lines.append(statusNote) }
        if let percent { lines.append("\(percent)%") }
        detail.stringValue = lines.joined(separator: "\n")
    }

    private func set(_ id: String, state: String, elapsed: String? = nil) {
        if let i = steps.firstIndex(where: { $0.id == id }) {
            steps[i].state = state
            if let elapsed { steps[i].elapsed = elapsed }
        }
    }

    private func workerFinished(code: Int32) {
        worker = nil
        if !finished {
            heading.stringValue = code == 0 ? "Yarn is ready" : "Setup stopped"
            subtitle.stringValue = code == 0 ? "Finished" : "Exit \(code)"
            cancel.title = "Close"
        }
        finished = true
    }

    @objc private func beginInstall() {
        welcome.isHidden = true
        summaryHint.isHidden = true
        fastBox.isHidden = true
        accurateBox.isHidden = true
        startButton.isHidden = true
        bar.isHidden = false
        list.isHidden = false
        detail.isHidden = false
        heading.stringValue = "Install Yarn"
        subtitle.stringValue = "Preparing…"
        cancel.title = "Cancel"
        launchWorker()
    }

    @objc private func cancelRun() {
        if let worker, worker.isRunning {
            worker.terminate()
        }
        NSApp.terminate(nil)
    }

    func windowWillClose(_ notification: Notification) {
        if let worker, worker.isRunning { worker.terminate() }
        NSApp.terminate(nil)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: Controller?

    func applicationDidFinishLaunching(_ notification: Notification) {
        var args = Array(CommandLine.arguments.dropFirst())
        var repo = FileManager.default.currentDirectoryPath
        var python = "/usr/bin/python3"
        if let i = args.firstIndex(of: "--repo"), i + 1 < args.count {
            repo = args[i + 1]
            args.removeSubrange(i...(i + 1))
        }
        if let i = args.firstIndex(of: "--python"), i + 1 < args.count {
            python = args[i + 1]
            args.removeSubrange(i...(i + 1))
        }
        let c = Controller(repo: repo, python: python)
        controller = c
        c.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

func memoryGB() -> Double {
    var value = UInt64(0)
    var size = MemoryLayout<UInt64>.size
    sysctlbyname("hw.memsize", &value, &size, nil, 0)
    return Double(value) / 1_073_741_824.0
}

@main
enum YarnSetup {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
