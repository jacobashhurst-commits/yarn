// Yarn in the Dock. The menu bar stays; this window is the place you look.
//
// It reads the same files the menu bar does, so the progress is the same
// story. It does not record audio itself. Start and stop call `rec`, which
// hands the microphone to the recorder job. A Dock app that captured sound
// would be the process macOS asks, and this one must never be that process.

import AppKit
import Darwin

struct Paths {
    let root: String
    let rec: String
    let ma: String

    static func load() -> Paths {
        let url = Bundle.main.url(forResource: "paths", withExtension: "plist")
        let dict = (url.flatMap { NSDictionary(contentsOf: $0) as? [String: String] }) ?? [:]
        return Paths(
            root: dict["root"] ?? (NSHomeDirectory() + "/meeting-archive"),
            rec: dict["rec"] ?? "",
            ma: dict["ma"] ?? ""
        )
    }
}

struct Snapshot {
    var recording = false
    var elapsed = "0:00"
    var mode = "room"
    var processing = false
    var paused = false
    var heading = "Nothing to process"
    var step = ""
    var detail = ""
    var percent: Int?
    var eta = ""
    var queue: [String] = []
    var waiting = 0
    var canProcess = false
    var meetings = 0
    var failed = 0
    var model = ""
    var ollamaDown = false
}

func yarn(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(
        srgbRed: CGFloat((hex >> 16) & 0xff) / 255,
        green: CGFloat((hex >> 8) & 0xff) / 255,
        blue: CGFloat(hex & 0xff) / 255,
        alpha: alpha
    )
}

func adaptive(light: UInt32, dark: UInt32) -> NSColor {
    NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? yarn(dark) : yarn(light)
    }
}

enum YarnColor {
    static let navy = adaptive(light: 0x0D244B, dark: 0xD6E4F5)
    static let ink = adaptive(light: 0x19293D, dark: 0xE7EEF6)
    static let slate = adaptive(light: 0x617188, dark: 0x9AAFC3)
    static let blue = yarn(0x167DFF)
    static let cloud = adaptive(light: 0xEDF6FF, dark: 0x172433)
    static let canvas = adaptive(light: 0xFFFFFF, dark: 0x101820)
    static let warn = adaptive(light: 0x8A5A16, dark: 0xE0B15A)
}

final class Panel: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let paths = Paths.load()
    let window: NSWindow
    let statusLabel = NSTextField(labelWithString: "Not recording")
    let bigLabel = NSTextField(labelWithString: "")
    let metaLabel = NSTextField(labelWithString: "")
    let recordButton = NSButton(title: "Start", target: nil, action: nil)
    let progressTitle = NSTextField(labelWithString: "Nothing to process")
    let progressStep = NSTextField(wrappingLabelWithString: "")
    let progressDetail = NSTextField(wrappingLabelWithString: "")
    let bar = NSProgressIndicator()
    let etaLabel = NSTextField(wrappingLabelWithString: "")
    let queueLabel = NSTextField(wrappingLabelWithString: "")
    let processButton = NSButton(title: "Process waiting", target: nil, action: nil)
    let resumeButton = NSButton(title: "Resume", target: nil, action: nil)
    let noteLabel = NSTextField(wrappingLabelWithString: "")
    let footer = NSTextField(labelWithString: "")
    var meetingCount = 0
    var countedAt = Date.distantPast
    var background = false

    override init() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 460),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "Yarn"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.backgroundColor = YarnColor.canvas
        build()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        background = CommandLine.arguments.contains("--background")
        if !background {
            show()
        }
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        refresh()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        show()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func show() {
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func build() {
        let content = NSView()
        window.contentView = content

        let mark = NSImageView()
        mark.image = markImage()
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.translatesAutoresizingMaskIntoConstraints = false
        mark.widthAnchor.constraint(equalToConstant: 108).isActive = true
        mark.heightAnchor.constraint(equalToConstant: 54).isActive = true

        let word = label("Yarn", size: 20, weight: .medium, color: YarnColor.navy, mono: true)
        let sub = label("Meetings stay on this Mac.", size: 13, weight: .regular, color: YarnColor.slate)
        sub.maximumNumberOfLines = 1
        sub.lineBreakMode = .byTruncatingTail
        sub.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        let titles = stack([word, sub], spacing: 2)
        titles.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)

        let header = stack([mark, titles], spacing: 12, horizontal: true)
        header.alignment = .centerY

        style(statusLabel, size: 12, weight: .medium, color: YarnColor.slate, mono: true)
        style(bigLabel, size: 28, weight: .semibold, color: YarnColor.navy, mono: true)
        style(metaLabel, size: 13, weight: .regular, color: YarnColor.slate)
        bigLabel.isHidden = true
        metaLabel.isHidden = true

        recordButton.target = self
        recordButton.action = #selector(toggleRecording)
        recordButton.bezelStyle = .rounded
        recordButton.bezelColor = YarnColor.blue
        recordButton.contentTintColor = .white
        recordButton.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        recordButton.keyEquivalent = "\r"

        let now = card(stack([statusLabel, bigLabel, metaLabel, recordButton], spacing: 6), owner: self)

        style(progressTitle, size: 15, weight: .semibold, color: YarnColor.ink)
        style(progressStep, size: 13, weight: .regular, color: YarnColor.ink)
        style(progressDetail, size: 12, weight: .regular, color: YarnColor.slate)
        style(etaLabel, size: 12, weight: .regular, color: YarnColor.slate)
        style(queueLabel, size: 12, weight: .regular, color: YarnColor.slate)
        progressStep.maximumNumberOfLines = 2
        progressDetail.maximumNumberOfLines = 3
        queueLabel.maximumNumberOfLines = 5
        bar.isIndeterminate = false
        bar.minValue = 0
        bar.maxValue = 100
        bar.isHidden = true

        processButton.target = self
        processButton.action = #selector(processWaiting)
        processButton.bezelStyle = .rounded
        processButton.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        processButton.isHidden = true
        resumeButton.target = self
        resumeButton.action = #selector(resume)
        resumeButton.bezelStyle = .rounded
        resumeButton.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        resumeButton.isHidden = true

        let progress = card(stack([
            progressTitle, progressStep, progressDetail, bar, etaLabel, queueLabel,
            processButton, resumeButton,
        ], spacing: 6), owner: self)

        let actions = stack([
            link("Ask Yarn", #selector(ask), self),
            link("Daily brief", #selector(brief), self),
            link("Library", #selector(library), self),
            link("Speakers", #selector(speakers), self),
            link("Transcripts", #selector(transcripts), self),
        ], spacing: 2)

        style(noteLabel, size: 12, weight: .regular, color: YarnColor.slate)
        noteLabel.maximumNumberOfLines = 2
        noteLabel.isHidden = true
        style(footer, size: 12, weight: .regular, color: YarnColor.slate, mono: true)

        let root = stack([header, now, progress, actions, noteLabel, footer], spacing: 14)
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor, constant: 8),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
        ])
        // Hug the content. A fixed tall window was centering this short page
        // and leaving a band of empty space under the traffic lights.
        content.layoutSubtreeIfNeeded()
        let height = root.fittingSize.height + 24
        window.setContentSize(NSSize(width: 400, height: max(420, height)))
    }

    func refresh() {
        render(read())
        let flag = paths.root + "/asks/.open-chat"
        if FileManager.default.fileExists(atPath: flag) {
            try? FileManager.default.removeItem(atPath: flag)
            AskChat.show(paths: paths)
        }
    }

    func render(_ snap: Snapshot) {
        if snap.recording {
            statusLabel.stringValue = "RECORDING"
            bigLabel.stringValue = snap.elapsed
            bigLabel.isHidden = false
            metaLabel.stringValue = snap.mode == "room" ? "In the room" : snap.mode
            metaLabel.isHidden = false
            recordButton.title = "Stop"
        } else {
            statusLabel.stringValue = "NOT RECORDING"
            bigLabel.isHidden = true
            metaLabel.isHidden = true
            recordButton.title = "Start"
        }
        recordButton.isEnabled = FileManager.default.isExecutableFile(atPath: paths.rec)

        progressTitle.stringValue = snap.heading
        progressStep.stringValue = snap.step
        progressStep.isHidden = snap.step.isEmpty
        progressDetail.stringValue = snap.detail
        progressDetail.isHidden = snap.detail.isEmpty
        if let percent = snap.percent {
            bar.isHidden = false
            bar.doubleValue = Double(percent)
        } else {
            bar.isHidden = true
        }
        etaLabel.stringValue = snap.eta
        etaLabel.isHidden = snap.eta.isEmpty
        queueLabel.stringValue = snap.queue.prefix(5).joined(separator: "\n")
        queueLabel.isHidden = snap.queue.isEmpty
        processButton.isHidden = !snap.canProcess
        resumeButton.isHidden = !snap.paused

        var foot = "\(snap.meetings) meeting\(snap.meetings == 1 ? "" : "s")"
        if !snap.model.isEmpty { foot += "  ·  \(snap.model)" }
        if snap.failed > 0 { foot += "  ·  \(snap.failed) kept their audio" }
        footer.stringValue = foot
        footer.textColor = snap.failed > 0 ? YarnColor.warn : YarnColor.slate
        if snap.ollamaDown && noteLabel.stringValue.isEmpty {
            noteLabel.stringValue = "Summaries need Ollama. Open it from the menu bar if a summary is waiting."
            noteLabel.isHidden = false
        }
    }

    func read() -> Snapshot {
        var snap = Snapshot()
        let root = paths.root
        if let lines = try? String(contentsOfFile: root + "/.recording", encoding: .utf8)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map(String.init),
           lines.count >= 6,
           let pid = Int32(lines[0]),
           kill(pid, 0) == 0 {
            snap.recording = true
            snap.mode = lines[4].isEmpty ? "room" : lines[4]
            if let started = TimeInterval(lines[5]) {
                let secs = max(0, Int(Date().timeIntervalSince1970 - started))
                snap.elapsed = String(format: "%d:%02d", secs / 60, secs % 60)
            }
        }

        let status = json(root + "/.processing-status.json")
        let now = Int(Date().timeIntervalSince1970)
        let updated = integer(status["updated_epoch"]) ?? 0
        let staleAfter = integer(status["stale_after_epoch"]) ?? 0
        let phase = text(status["phase"])
        if !phase.isEmpty && updated > 0 && (now - updated < 7200 || (staleAfter > 0 && now < staleAfter)) {
            snap.processing = true
        }
        let control = json(root + "/.processing-control.json")
        snap.paused = boolean(control["paused"])

        let queue = json(root + "/.queue-status.json")
        let remaining = integer(queue["remaining"]) ?? countFiles(root + "/recordings/inbox", suffix: ".json")
        snap.waiting = snap.processing ? (integer(status["remaining"]) ?? remaining) : remaining

        if snap.processing {
            let title = text(status["title"])
            let current = integer(status["current"]) ?? 0
            let total = integer(status["total"]) ?? 0
            let length = text(status["recording_length"])
            var heading = title.isEmpty ? shortID(text(status["meeting_id"])) : title
            if !length.isEmpty { heading += " (\(length))" }
            if current > 0 && total > 0 {
                heading = "Meeting \(current) of \(total) — \(heading)"
            }
            snap.heading = heading
            let step = text(status["step"]).isEmpty ? phase : text(status["step"])
            let started = integer(status["phase_started_epoch"]) ?? 0
            let elapsed = started > 0 ? max(0, now - started) : 0
            snap.step = step.isEmpty ? "" : "\(step) — \(span(elapsed))"
            snap.detail = text(status["detail"])
            let expected = integer(status["expected_s"]) ?? 0
            let indeterminate = boolean(status["indeterminate"])
            if indeterminate && expected >= 30 {
                snap.percent = min(95, elapsed * 100 / expected)
                let left = expected - elapsed
                snap.eta = left > 0 ? "About \(span(left)) left" : "Taking longer than usual"
            } else if let percent = integer(status["percent"]), !indeterminate {
                snap.percent = min(100, max(0, percent))
            }
            if snap.eta.isEmpty { snap.eta = text(status["eta"]) }
        } else if snap.paused {
            snap.heading = "Paused"
        } else {
            var idle = "Nothing to process"
            let last = text(queue["last_id"])
            if !last.isEmpty {
                idle += " — last finished \(shortID(last))"
                if let done = integer(queue["last_finished_epoch"]), done > 0 {
                    let formatter = DateFormatter()
                    formatter.dateFormat = "HH:mm"
                    idle += " at \(formatter.string(from: Date(timeIntervalSince1970: TimeInterval(done))))"
                }
            }
            snap.heading = idle
        }

        if let lines = try? String(contentsOfFile: root + "/.queue-lines.txt", encoding: .utf8) {
            snap.queue = lines.split(separator: "\n").map { "• \($0)" }.filter { $0.count > 2 }
        }
        snap.canProcess = !snap.processing && !snap.paused && snap.waiting > 0
        if snap.waiting > 0 && !snap.processing {
            let when = text(json(root + "/.yarn-settings.json")["summarize_when"])
            var note = "\(snap.waiting) waiting"
            if when == "evening" { note += " until evening" }
            if when == "manual" { note += " until you process" }
            if snap.queue.isEmpty { snap.queue = [note] }
        }

        if countedAt.timeIntervalSinceNow < -30 || meetingCount == 0 {
            meetingCount = countFiles(root + "/transcripts", suffix: ".json")
            countedAt = Date()
        }
        snap.meetings = meetingCount
        snap.failed = countFiles(root + "/recordings/failed", suffix: ".json")
        let model = text(json(root + "/.yarn-settings.json")["summary_model"])
        if model.contains("4b") {
            snap.model = "Fast"
        } else if model.contains("9b") {
            snap.model = "Accurate"
        }
        let summaries = integer(queue["summaries_pending"]) ?? 0
        let ollama = NSWorkspace.shared.runningApplications.contains {
            ($0.localizedName ?? "").localizedCaseInsensitiveContains("Ollama")
        }
        snap.ollamaDown = summaries > 0 && !ollama
        return snap
    }

    @objc func toggleRecording() {
        let snap = read()
        if snap.recording {
            launch(paths.rec, ["stop"])
        } else {
            launch(paths.rec, ["start", "room"])
        }
    }

    @objc func processWaiting() { launch(paths.ma, ["process"]) }
    @objc func resume() { launch(paths.ma, ["resume"]) }

    @objc func ask() {
        AskChat.show(paths: paths)
    }

    @objc func brief() {
        note("Writing today's brief. It usually takes about a minute.")
        launch(paths.ma, ["brief", "--open", "--force"])
    }

    @objc func library() {
        note("Opening the library…")
        let ma = paths.ma
        let page = paths.root + "/library.html"
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: ma)
            task.arguments = ["library", "--html"]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            try? task.run()
            task.waitUntilExit()
            DispatchQueue.main.async {
                NSWorkspace.shared.open(URL(fileURLWithPath: page))
            }
        }
    }

    @objc func speakers() {
        note("Opening speakers…")
        let ma = paths.ma
        let page = paths.root + "/speakers/library.html"
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: ma)
            task.arguments = ["voices", "--html"]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            try? task.run()
            task.waitUntilExit()
            DispatchQueue.main.async {
                NSWorkspace.shared.open(URL(fileURLWithPath: page))
            }
        }
    }

    @objc func transcripts() {
        NSWorkspace.shared.open(URL(fileURLWithPath: paths.root + "/transcripts"))
    }

    func note(_ text: String) {
        noteLabel.stringValue = text
        noteLabel.isHidden = text.isEmpty
    }

    func launch(_ bin: String, _ args: [String]) {
        guard FileManager.default.isExecutableFile(atPath: bin) else {
            note("That action isn't available yet.")
            return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: bin)
        task.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["MA_BIN"] = paths.ma
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        task.environment = env
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        try? task.run()
    }
}

private func stack(_ views: [NSView], spacing: CGFloat, horizontal: Bool = false) -> NSStackView {
    let view = NSStackView(views: views)
    view.orientation = horizontal ? .horizontal : .vertical
    view.alignment = horizontal ? .centerY : .leading
    view.spacing = spacing
    view.translatesAutoresizingMaskIntoConstraints = false
    if !horizontal {
        for child in views {
            child.translatesAutoresizingMaskIntoConstraints = false
            child.widthAnchor.constraint(equalTo: view.widthAnchor).isActive = true
        }
    }
    return view
}

final class CardView: NSView {
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        layer?.backgroundColor = YarnColor.cloud.cgColor
        layer?.cornerRadius = 12
    }
}

private func card(_ content: NSView, owner: AnyObject) -> NSView {
    let box = CardView()
    box.wantsLayer = true
    content.translatesAutoresizingMaskIntoConstraints = false
    box.addSubview(content)
    NSLayoutConstraint.activate([
        content.topAnchor.constraint(equalTo: box.topAnchor, constant: 14),
        content.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 14),
        content.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -14),
        content.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -14),
    ])
    for case let button as NSButton in content.subviews where button.target == nil && button.action != nil {
        button.target = owner
    }
    return box
}

private func label(_ text: String, size: CGFloat, weight: NSFont.Weight, color: NSColor, mono: Bool = false) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    style(field, size: size, weight: weight, color: color, mono: mono)
    return field
}

private func style(_ field: NSTextField, size: CGFloat, weight: NSFont.Weight, color: NSColor, mono: Bool = false) {
    field.font = mono
        ? NSFont.monospacedSystemFont(ofSize: size, weight: weight)
        : NSFont.systemFont(ofSize: size, weight: weight)
    field.textColor = color
    field.maximumNumberOfLines = 2
    field.lineBreakMode = .byWordWrapping
}

private func link(_ title: String, _ action: Selector, _ target: AnyObject) -> NSButton {
    let button = NSButton(title: title, target: target, action: action)
    button.isBordered = false
    button.alignment = .left
    button.font = NSFont.systemFont(ofSize: 15, weight: .medium)
    button.contentTintColor = YarnColor.navy
    return button
}

private func json(_ path: String) -> [String: Any] {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return [:]
    }
    return obj
}

private func text(_ value: Any?) -> String {
    if let value = value as? String { return value }
    if let value = value as? NSNumber { return value.stringValue }
    return ""
}

private func integer(_ value: Any?) -> Int? {
    if let value = value as? Int { return value }
    if let value = value as? NSNumber { return value.intValue }
    if let value = value as? String { return Int(value) }
    return nil
}

private func boolean(_ value: Any?) -> Bool {
    if let value = value as? Bool { return value }
    if let value = value as? NSNumber { return value.boolValue }
    if let value = value as? String { return value == "true" || value == "1" }
    return false
}

private func span(_ seconds: Int) -> String {
    if seconds >= 3600 { return String(format: "%dh %02dm", seconds / 3600, (seconds % 3600) / 60) }
    if seconds >= 60 { return "\(seconds / 60)m" }
    return "\(seconds)s"
}

private func shortID(_ id: String) -> String {
    guard id.count >= 16 else { return id }
    let date = id.dropFirst(5).prefix(5)
    let time = id.dropFirst(11).prefix(5).replacingOccurrences(of: "-", with: ":")
    return "\(date) \(time)"
}

private func countFiles(_ dir: String, suffix: String) -> Int {
    guard let enumerator = FileManager.default.enumerator(atPath: dir) else { return 0 }
    var count = 0
    while let name = enumerator.nextObject() as? String {
        if name.hasSuffix(suffix) { count += 1 }
    }
    return count
}

private func markImage() -> NSImage? {
    guard let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png") else { return nil }
    return NSImage(contentsOf: url)
}

@main
enum YarnDock {
    static func main() {
        let app = NSApplication.shared
        let delegate = Panel()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
