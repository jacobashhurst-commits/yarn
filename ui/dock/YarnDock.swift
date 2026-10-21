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
    var heading = ""
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
    var tokens = 0
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
    let progressTitle = NSTextField(labelWithString: "")
    let progressStep = NSTextField(wrappingLabelWithString: "")
    let progressDetail = NSTextField(wrappingLabelWithString: "")
    let bar = YarnBar()
    let percentLabel = NSTextField(labelWithString: "")
    var barMeeting = ""
    var barFloor = 0
    let etaLabel = NSTextField(wrappingLabelWithString: "")
    let queueLabel = NSTextField(wrappingLabelWithString: "")
    let processButton = NSButton(title: "Process waiting", target: nil, action: nil)
    let resumeButton = NSButton(title: "Resume", target: nil, action: nil)
    let noteLabel = NSTextField(wrappingLabelWithString: "")
    let footer = NSTextField(labelWithString: "")
    var meetingCount = 0
    var countedAt = Date.distantPast
    var background = false
    var rootStack: NSStackView?
    var progressCard: NSView?

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
        YarnStatus.shared.start()
        if !background {
            show()
        }
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        refresh()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        YarnStatus.shared.start()
        show()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    private var offeredWelcome = false
    private var welcomeController: YarnWelcomeWindow?

    func show() {
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        offerWelcome()
    }

    private func offerWelcome() {
        if offeredWelcome { return }
        offeredWelcome = true
        let path = paths.root + "/.yarn-settings.json"
        if YarnWelcomeSettings.consumeHandoff(path) { return }
        if YarnWelcomeSettings.isHidden(path) { return }
        let controller = YarnWelcomeWindow(settingsPath: path) { [weak self] in
            self?.welcomeController?.close()
            self?.welcomeController = nil
        }
        welcomeController = controller
        controller.showWindow(nil)
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
        let sub = label("Everything stays on this Mac", size: 13, weight: .regular, color: YarnColor.slate)
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
        recordButton.setContentHuggingPriority(.required, for: .vertical)
        recordButton.heightAnchor.constraint(equalToConstant: 32).isActive = true

        let recordingStack = stack([statusLabel, bigLabel, metaLabel, recordButton], spacing: 6)
        recordingStack.detachesHiddenViews = true
        let now = card(recordingStack, owner: self)

        style(progressTitle, size: 15, weight: .semibold, color: YarnColor.ink)
        style(progressStep, size: 13, weight: .regular, color: YarnColor.ink)
        style(progressDetail, size: 12, weight: .regular, color: YarnColor.slate)
        style(etaLabel, size: 12, weight: .regular, color: YarnColor.slate)
        style(queueLabel, size: 12, weight: .regular, color: YarnColor.slate)
        progressStep.maximumNumberOfLines = 2
        progressDetail.maximumNumberOfLines = 4
        queueLabel.maximumNumberOfLines = 5
        bar.isHidden = true
        bar.heightAnchor.constraint(equalToConstant: 8).isActive = true
        style(percentLabel, size: 12, weight: .medium, color: YarnColor.ink, mono: true)
        percentLabel.alignment = .right
        percentLabel.isHidden = true
        percentLabel.widthAnchor.constraint(equalToConstant: 48).isActive = true
        bar.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let barRow = stack([bar, percentLabel], spacing: 8, horizontal: true)
        barRow.distribution = .fill

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
            progressTitle, progressStep, progressDetail, barRow, etaLabel, queueLabel,
            processButton, resumeButton,
        ], spacing: 6), owner: self)
        progress.isHidden = true
        progressCard = progress

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
        footer.preferredMaxLayoutWidth = 340
        footer.maximumNumberOfLines = 2

        let root = stack([header, now, progress, actions, noteLabel, footer], spacing: 14)
        root.detachesHiddenViews = true
        root.translatesAutoresizingMaskIntoConstraints = false
        root.setContentHuggingPriority(.required, for: .vertical)
        rootStack = root
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor, constant: 8),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            root.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -16),
        ])
        fitWindow()
    }

    /// The window is only as tall as what is on screen. A fixed height was
    /// stretching the recording card into a blank band whenever the timer
    /// was hidden.
    func fitWindow() {
        guard let rootStack, let content = window.contentView else { return }
        content.layoutSubtreeIfNeeded()
        let height = ceil(rootStack.fittingSize.height) + 24
        let current = window.contentRect(forFrameRect: window.frame).height
        guard abs(current - height) > 2 else { return }
        var frame = window.frame
        let next = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: 400, height: height))
        frame.origin.y += frame.size.height - next.height
        frame.size = next.size
        window.setFrame(frame, display: true)
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
            metaLabel.stringValue = snap.mode == "call" ? "On a call" : "In the room"
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
            bar.fraction = CGFloat(percent) / 100
            percentLabel.stringValue = "\(percent)%"
            percentLabel.isHidden = false
        } else {
            bar.isHidden = true
            percentLabel.isHidden = true
        }
        etaLabel.stringValue = snap.eta
        etaLabel.isHidden = snap.eta.isEmpty
        queueLabel.stringValue = snap.queue.prefix(5).joined(separator: "\n")
        queueLabel.isHidden = snap.queue.isEmpty
        processButton.isHidden = !snap.canProcess
        resumeButton.isHidden = !snap.paused
        let showProgress = snap.processing || snap.paused || snap.waiting > 0
        progressCard?.isHidden = !showProgress
        progressTitle.isHidden = snap.heading.isEmpty

        var foot = "\(snap.meetings) meeting\(snap.meetings == 1 ? "" : "s")"
        let tokens = NumberFormatter.localizedString(from: NSNumber(value: snap.tokens), number: .decimal)
        foot += "  ·  \(tokens) tokens saved"
        if !snap.model.isEmpty { foot += "  ·  \(snap.model)" }
        if snap.failed > 0 { foot += "  ·  \(snap.failed) kept their audio" }
        footer.stringValue = foot
        footer.textColor = snap.failed > 0 ? YarnColor.warn : YarnColor.slate
        fitWindow()
        let idle = noteLabel.stringValue.isEmpty
            || noteLabel.stringValue == restingNote
            || noteLabel.stringValue == "Opening the library…"
        if snap.ollamaDown && idle {
            note("Summaries need Ollama. Open it from the menu bar if a summary is waiting.")
        } else if idle {
            note(restingNote)
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
                heading = "Meeting \(current) of \(total): \(heading)"
            }
            snap.heading = heading
            let step = text(status["step"]).isEmpty ? phase : text(status["step"])
            let started = integer(status["phase_started_epoch"]) ?? 0
            let elapsed = started > 0 ? max(0, now - started) : 0
            snap.step = step.isEmpty ? "" : "\(step), \(span(elapsed))"
            let hint = phaseHint(phase)
            let written = text(status["detail"])
            if hint.isEmpty {
                snap.detail = written
            } else if written.isEmpty {
                snap.detail = hint
            } else {
                snap.detail = hint + "\n" + written
            }
            let expected = integer(status["expected_s"]) ?? 0
            let indeterminate = boolean(status["indeterminate"])
            let stage = integer(status["percent"]) ?? 0
            if !indeterminate, expected > 0 {
                let left = expected - elapsed
                snap.eta = left > 0 ? "About \(span(left)) left" : "Taking longer than usual"
            }
            let meeting = text(status["meeting_id"])
            snap.percent = forwardPercent(
                meeting: meeting,
                phase: phase,
                stage: stage,
                elapsed: elapsed,
                expected: expected,
                measured: !indeterminate
            )
            if snap.eta.isEmpty { snap.eta = text(status["eta"]) }
        } else if snap.paused {
            snap.heading = "Paused"
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
            if snap.heading.isEmpty { snap.heading = note }
        }

        if countedAt.timeIntervalSinceNow < -30 || meetingCount == 0 {
            meetingCount = countFiles(root + "/transcripts", suffix: ".json")
            countedAt = Date()
        }
        snap.meetings = meetingCount
        snap.failed = countFiles(root + "/recordings/failed", suffix: ".json")
        let model = text(json(root + "/.yarn-settings.json")["summary_model"])
        snap.model = model
        snap.tokens = integer(json(root + "/.tokens-saved.json")["total"]) ?? 0
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
                self.note(self.restingNote)
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
        let ma = paths.ma
        let page = paths.root + "/transcripts.html"
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: ma)
            task.arguments = ["transcripts", "--html"]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            try? task.run()
            task.waitUntilExit()
            DispatchQueue.main.async {
                NSWorkspace.shared.open(URL(fileURLWithPath: page))
                self.note(self.restingNote)
            }
        }
    }

    /// One fill for the whole meeting. A stage with no measured fraction
    /// creeps forward inside its own slice, and the number never goes back.
    func forwardPercent(
        meeting: String,
        phase: String,
        stage: Int,
        elapsed: Int,
        expected: Int,
        measured: Bool
    ) -> Int {
        if meeting != barMeeting {
            barMeeting = meeting
            barFloor = 0
        }
        let (start, end) = phaseBand(phase)
        let span = max(1, end - start)
        let inner: Double
        if measured {
            inner = Double(min(100, max(0, stage))) / 100
        } else if expected > 0 {
            inner = min(0.92, Double(elapsed) / Double(expected))
        } else {
            inner = min(0.9, Double(elapsed) / 40)
        }
        let value = start + Int((Double(span) * inner).rounded())
        barFloor = max(barFloor, min(end, value))
        return barFloor
    }

    let restingNote = "How good this is free!"

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

private func phaseBand(_ phase: String) -> (Int, Int) {
    switch phase {
    case "Preparing audio": return (2, 8)
    case "Transcribing": return (8, 62)
    case "Identifying speakers": return (62, 78)
    case "Recognising known speakers": return (78, 84)
    case "Cleaning transcript": return (84, 88)
    case "Writing summary": return (88, 96)
    case "Indexing", "Indexing for search": return (96, 99)
    case "Saving transcript": return (99, 100)
    default: return (8, 96)
    }
}

final class YarnBar: NSView {
    var fraction: CGFloat = 0 {
        didSet { needsDisplay = true }
    }

    override var intrinsicContentSize: NSSize {
        NSSize(width: NSView.noIntrinsicMetric, height: 8)
    }

    override func draw(_ dirtyRect: NSRect) {
        let radius = bounds.height / 2
        let track = NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius)
        NSColor.white.withAlphaComponent(0.14).setFill()
        track.fill()
        let width = bounds.width * min(1, max(0, fraction))
        guard width > 0.5 else { return }
        track.addClip()
        YarnColor.blue.setFill()
        NSBezierPath(
            roundedRect: NSRect(x: 0, y: 0, width: width, height: bounds.height),
            xRadius: radius,
            yRadius: radius
        ).fill()
    }
}

private func phaseHint(_ phase: String) -> String {
    switch phase {
    case "Transcribing":
        return "Listening through the recording and writing down what was said."
    case "Identifying speakers":
        return "Separating the voices, so each person can be named afterwards."
    case "Recognising known speakers":
        return "Comparing these voices with people Yarn already knows."
    case "Cleaning transcript":
        return "Tidying the words before the transcript is saved."
    case "Indexing", "Indexing for search":
        return "Filing this meeting so Ask Yarn can search it."
    case "Writing summary":
        return "Writing the summary on this Mac."
    default:
        return ""
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
    box.setContentHuggingPriority(.required, for: .vertical)
    box.setContentCompressionResistancePriority(.required, for: .vertical)
    content.setContentHuggingPriority(.required, for: .vertical)
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
    let button = HoverLink(title: title, target: target, action: action)
    button.font = NSFont.systemFont(ofSize: 15, weight: .medium)
    button.heightAnchor.constraint(equalToConstant: 30).isActive = true
    return button
}

final class HoverLink: NSButton {
    private var hovering = false

    init(title: String, target: AnyObject, action: Selector) {
        super.init(frame: .zero)
        self.title = title
        self.target = target
        self.action = action
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 6
    }

    required init?(coder: NSCoder) { nil }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .pointingHand)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect, .cursorUpdate],
            owner: self
        ))
    }

    override func cursorUpdate(with event: NSEvent) {
        NSCursor.pointingHand.set()
    }

    override func mouseEntered(with event: NSEvent) {
        hovering = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        hovering = false
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        if hovering {
            yarn(0x167DFF, 0.16).setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 0, dy: 1), xRadius: 6, yRadius: 6).fill()
        }
        let shown = NSAttributedString(
            string: title,
            attributes: [
                .font: font ?? NSFont.systemFont(ofSize: 15, weight: .medium),
                .foregroundColor: YarnColor.navy,
            ]
        )
        let size = shown.size()
        shown.draw(in: NSRect(x: 8, y: (bounds.height - size.height) / 2, width: bounds.width - 16, height: size.height))
    }
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

/// Cmd-C and Cmd-V only fire when the app has an Edit menu. Without it,
/// the chat field and the answer text cannot copy or paste.
private func editMenu() -> NSMenu {
    let main = NSMenu()
    let editItem = NSMenuItem()
    editItem.title = "Edit"
    let edit = NSMenu(title: "Edit")
    func item(_ title: String, _ action: Selector, _ key: String, shift: Bool = false) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
        entry.keyEquivalentModifierMask = shift ? [.command, .shift] : [.command]
        return entry
    }
    edit.addItem(item("Undo", Selector(("undo:")), "z"))
    edit.addItem(item("Redo", Selector(("redo:")), "Z", shift: true))
    edit.addItem(.separator())
    edit.addItem(item("Cut", #selector(NSText.cut(_:)), "x"))
    edit.addItem(item("Copy", #selector(NSText.copy(_:)), "c"))
    edit.addItem(item("Paste", #selector(NSText.paste(_:)), "v"))
    edit.addItem(item("Select All", #selector(NSText.selectAll(_:)), "a"))
    editItem.submenu = edit
    main.addItem(editItem)
    return main
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
        app.mainMenu = editMenu()
        app.run()
    }
}
