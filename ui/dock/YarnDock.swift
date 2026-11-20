// Yarn in the Dock. The menu bar stays; this window is the place you look.
//
// It reads the same files the menu bar does, so the progress is the same
// story. The microphone still belongs to the recorder job. The sound a
// video call plays is recorded in this app, because that permission is
// per app and a second helper kept asking after Yarn was already allowed.
// Recording in the room never starts that capture.

import AppKit
import Darwin

struct Paths {
    let root: String
    let rec: String
    let ma: String

    static func load() -> Paths {
        // Paths sit outside the app. Writing them into the bundle would
        // change its signature, and the screen-recording switch would stop
        // matching the Yarn the user just turned on.
        let external = URL(fileURLWithPath: NSHomeDirectory() + "/Library/Application Support/Yarn/paths.plist")
        let bundled = Bundle.main.url(forResource: "paths", withExtension: "plist")
        let url = FileManager.default.fileExists(atPath: external.path) ? external : bundled
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
    var identifying = false
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
    var modelTip = ""
    var tokens = 0
    var space = ""
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
    let roomButton = NSButton(title: "In the room", target: nil, action: nil)
    let headphonesButton = NSButton(title: "Headphones", target: nil, action: nil)
    let recordButton = NSButton(title: "Stop", target: nil, action: nil)
    var startRow: NSStackView?
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
    // The footer is separate labels so tokens, disk and the model can each
    // explain themselves on hover. One label has one tooltip.
    let footMeetings = NSTextField(labelWithString: "")
    let footTokens = NSTextField(labelWithString: "")
    let footSpace = NSTextField(labelWithString: "")
    let footModel = NSTextField(labelWithString: "")
    let footRest = NSTextField(labelWithString: "")
    var tokensTip = ""
    var spaceTip = ""
    var savingsRunning = false
    var meetingCount = 0
    var spaceSaved = ""
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
        if CommandLine.arguments.contains("--prepare-call-audio") {
            prepareCallAudioAndQuit()
            return
        }
        background = CommandLine.arguments.contains("--background")
        YarnHotKey.shared.onPress = { [weak self] mode in
            guard let self else { return }
            if self.read().recording {
                self.launch(self.paths.rec, ["stop"])
            } else if mode == "room" || mode == "call" {
                self.launch(self.paths.rec, ["start", mode])
            }
        }
        YarnStatus.shared.start()
        CallAudio.shared.begin(archiveRoot: paths.root)
        if !background {
            show()
        }
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in
            Task { @MainActor in
                CallAudio.shared.poll()
            }
        }
        refresh()
    }

    private func prepareCallAudioAndQuit() {
        // One ScreenCaptureKit attempt puts Yarn in Settings. Setup then opens
        // that row when the user still needs to flip the switch.
        NSApp.activate(ignoringOtherApps: true)
        Task { @MainActor in
            let granted = await CallAudio.prepareAccess()
            let dir = NSHomeDirectory() + "/Library/Application Support/Yarn"
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            let body = granted ? "granted\n" : "needs-switch\n"
            try? body.write(
                toFile: dir + "/call-audio-access",
                atomically: true,
                encoding: .utf8
            )
            NSApp.terminate(nil)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        YarnStatus.shared.start()
        show()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    /// The sound of a headphone call is captured inside this app. Cmd-Q in
    /// the middle of one would cut the other side of the meeting off with
    /// nothing on screen to say so. The microphone job is separate and is
    /// not affected either way.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let snap = read()
        guard snap.recording, snap.mode == "call" else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "A headphone recording is running"
        alert.informativeText = "Quitting Yarn now stops recording the other people on the call. Stop the recording first to keep the whole meeting."
        alert.addButton(withTitle: "Keep Recording")
        alert.addButton(withTitle: "Quit Anyway")
        return alert.runModal() == .alertFirstButtonReturn ? .terminateCancel : .terminateNow
    }

    private var offeredWelcome = false
    private var welcomeController: YarnWelcomeWindow?

    private var placed = false

    func show() {
        // Where it was last left. Centring every time moved the window back
        // each time Yarn was clicked in the Dock.
        if !placed {
            placed = true
            if !window.setFrameUsingName("YarnMain") { window.center() }
            window.setFrameAutosaveName("YarnMain")
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        // The saved frame is where it was left. The width follows the footer,
        // so a restored frame does not keep the old gap on the right.
        fitWindow()
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

        primeRecordButton(roomButton, action: #selector(startInRoom))
        primeRecordButton(headphonesButton, action: #selector(startHeadphones))
        primeRecordButton(recordButton, action: #selector(toggleRecording))
        // The titles are short because they sit side by side. Read aloud,
        // "Headphones" does not say that it starts a recording.
        roomButton.setAccessibilityLabel("Start recording, in the room")
        roomButton.setAccessibilityHelp("Records this Mac\u{2019}s microphone. For a meeting in person.")
        headphonesButton.setAccessibilityLabel("Start recording, headphones call")
        headphonesButton.setAccessibilityHelp("Records your microphone and the call audio this Mac is playing.")
        recordButton.setAccessibilityLabel("Stop recording")
        bar.setAccessibilityLabel("Processing progress")
        recordButton.isHidden = true

        let starts = NSStackView(views: [roomButton, headphonesButton])
        starts.orientation = .horizontal
        starts.distribution = .fillEqually
        starts.spacing = 8
        starts.translatesAutoresizingMaskIntoConstraints = false
        startRow = starts

        let recordingStack = stack([statusLabel, bigLabel, metaLabel, starts, recordButton], spacing: 6)
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
            link("Settings", #selector(settings), self),
        ], spacing: 2)

        style(noteLabel, size: 12, weight: .regular, color: YarnColor.slate)
        noteLabel.maximumNumberOfLines = 2
        noteLabel.wantsLayer = true
        quips = QuipLine(label: noteLabel)
        for part in [footMeetings, footTokens, footSpace, footModel, footRest] {
            style(part, size: 12, weight: .regular, color: YarnColor.slate, mono: true)
            part.maximumNumberOfLines = 1
            part.lineBreakMode = .byTruncatingTail
            part.setContentHuggingPriority(.required, for: .horizontal)
        }
        // One line. The count, the space and the model stay whole. A rare
        // "kept their audio" note is the only part that may shorten.
        footMeetings.setContentCompressionResistancePriority(.required, for: .horizontal)
        footTokens.setContentCompressionResistancePriority(.required, for: .horizontal)
        footSpace.setContentCompressionResistancePriority(.required, for: .horizontal)
        footModel.setContentCompressionResistancePriority(.required, for: .horizontal)
        footRest.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        footTokens.setAccessibilityHelp("Approximate cost if this had run on a hosted AI model")
        footSpace.setAccessibilityHelp("Disk space saved compared with keeping video recordings")
        let footer = stack(
            [footMeetings, footTokens, footSpace, footModel, footRest],
            spacing: 0,
            horizontal: true
        )
        footer.detachesHiddenViews = true

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

    /// The window is only as tall as what is on screen, and only as wide as
    /// the footer. A fixed width left an empty gap after the model name.
    func fitWindow() {
        guard let rootStack, let content = window.contentView else { return }
        let width = fittedWidth()
        noteLabel.preferredMaxLayoutWidth = width - 40
        content.layoutSubtreeIfNeeded()
        let height = ceil(rootStack.fittingSize.height) + 24
        let current = window.contentRect(forFrameRect: window.frame)
        guard abs(current.height - height) > 2 || abs(current.width - width) > 2 else { return }
        var frame = window.frame
        let next = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: width, height: height))
        frame.origin.y += frame.size.height - next.height
        frame.size = next.size
        window.setFrame(frame, display: true)
    }

    /// Footer text, or the title and the two record buttons when the footer
    /// is shorter than those.
    private func fittedWidth() -> CGFloat {
        func words(_ text: String, _ font: NSFont) -> CGFloat {
            ceil((text as NSString).size(withAttributes: [.font: font]).width)
        }
        let footer = [footMeetings, footTokens, footSpace, footModel, footRest].reduce(CGFloat(0)) { sum, field in
            guard !field.isHidden, !field.stringValue.isEmpty, let font = field.font else { return sum }
            field.invalidateIntrinsicContentSize()
            let intrinsic = field.intrinsicContentSize.width
            let drawn = words(field.stringValue, font)
            let width = intrinsic > drawn && intrinsic < 800 ? intrinsic : drawn
            return sum + width
        }
        let header = 108 + 12 + words("Everything stays on this Mac", NSFont.systemFont(ofSize: 13))
        let buttonWidth = { (button: NSButton) -> CGFloat in
            let size = button.intrinsicContentSize.width
            return size > 0 && size < 400 ? size : 0
        }
        let buttons = buttonWidth(roomButton) + buttonWidth(headphonesButton) + 8 + 28
        return max(header, footer, buttons) + 40
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
        let canRecord = FileManager.default.isExecutableFile(atPath: paths.rec)
        if snap.recording {
            statusLabel.stringValue = "RECORDING"
            bigLabel.stringValue = snap.elapsed
            bigLabel.isHidden = false
            metaLabel.stringValue = snap.mode == "call" ? "Headphones" : "In the room"
            metaLabel.isHidden = false
            startRow?.isHidden = true
            recordButton.isHidden = false
            recordButton.title = "Stop"
            recordButton.keyEquivalent = "\r"
        } else if snap.identifying {
            statusLabel.stringValue = "WAITING"
            bigLabel.isHidden = true
            metaLabel.stringValue = "Waiting to identify speakers"
            metaLabel.isHidden = false
            startRow?.isHidden = false
            recordButton.isHidden = true
            recordButton.keyEquivalent = ""
        } else {
            statusLabel.stringValue = "NOT RECORDING"
            bigLabel.isHidden = true
            metaLabel.isHidden = true
            startRow?.isHidden = false
            recordButton.isHidden = true
            recordButton.keyEquivalent = ""
        }
        recordButton.isEnabled = canRecord
        roomButton.isEnabled = canRecord
        headphonesButton.isEnabled = canRecord

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

        footMeetings.stringValue = "\(snap.meetings) meeting\(snap.meetings == 1 ? "" : "s")"
        footTokens.stringValue = " · \(formatTokens(snap.tokens)) tokens"
        footTokens.toolTip = tokensTip.isEmpty ? nil : tokensTip
        footSpace.stringValue = snap.space.isEmpty ? "" : " · \(snap.space)"
        footSpace.isHidden = snap.space.isEmpty
        footSpace.toolTip = spaceTip.isEmpty ? nil : spaceTip
        footModel.stringValue = snap.model.isEmpty ? "" : " · \(snap.model)"
        footModel.isHidden = snap.model.isEmpty
        footModel.toolTip = snap.modelTip.isEmpty ? nil : snap.modelTip
        footModel.setAccessibilityHelp(snap.modelTip)
        let rest = snap.failed > 0 ? " · \(snap.failed) kept their audio" : ""
        footRest.stringValue = rest
        footRest.isHidden = rest.isEmpty
        let footColor = snap.failed > 0 ? YarnColor.warn : YarnColor.slate
        for part in [footMeetings, footTokens, footSpace, footModel, footRest] {
            part.textColor = footColor
        }
        if snap.ollamaDown {
            quips.hold(
                "Summaries need Ollama. Open it from the menu bar if a summary is waiting.",
                sticky: true,
                reason: "ollama"
            )
        } else {
            quips.releaseSticky(reason: "ollama")
        }
        fitWindow()
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
        snap.identifying = identifyWaitAlive(root + "/.identify-wait")

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
            // The queue total grows when another meeting arrives during a
            // long stage. The stage's own status file can still say 1 of 1.
            let total = max(integer(status["total"]) ?? 0, integer(queue["total"]) ?? 0)
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
            // The same rule as pipeline.meeting_percent, worked out here each
            // second because a long silent step does not rewrite the status file.
            let after = integer(status["after_s"]) ?? 0
            let before = integer(status["before_s"]) ?? 0
            let learned = status["estimate_learned"] == nil ? true : boolean(status["estimate_learned"])
            snap.eta = meetingEta(
                expected: expected,
                after: after,
                learned: learned,
                elapsed: elapsed,
                stage: stage,
                indeterminate: indeterminate
            )
            let meeting = text(status["meeting_id"])
            snap.percent = forwardPercent(
                meeting: meeting,
                before: before,
                expected: expected,
                after: after,
                phase: phase,
                stage: stage,
                elapsed: elapsed,
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
            refreshSavings()
            countedAt = Date()
        }
        snap.meetings = meetingCount
        snap.space = spaceSaved
        snap.failed = countFiles(root + "/recordings/failed", suffix: ".json")
        let settings = json(root + "/.yarn-settings.json")
        let summary = summaryChoice(text(settings["summary_model"]))
        snap.model = summary.label
        snap.modelTip = summary.tip
        let hot = text(settings["record_hotkey"])
        var startMode = text(settings["record_mode"])
        if startMode != "room" && startMode != "call" {
            startMode = hot == "call" ? "call" : "room"
        }
        let shortcutOn = hot == "on" || hot == "room" || hot == "call"
        YarnHotKey.shared.apply(shortcutOn ? startMode : "off")
        snap.tokens = integer(json(root + "/.tokens-saved.json")["total"]) ?? 0
        let summaries = integer(queue["summaries_pending"]) ?? 0
        let ollama = NSWorkspace.shared.runningApplications.contains {
            ($0.localizedName ?? "").localizedCaseInsensitiveContains("Ollama")
        }
        snap.ollamaDown = summaries > 0 && !ollama
        return snap
    }

    /// The figures and their explanations come from `ma savings --json`, so
    /// the Dock, the command line and the tests all use one calculation.
    /// It runs off the main thread: starting Python takes about a second.
    private func refreshSavings() {
        let ma = paths.ma
        guard !savingsRunning, FileManager.default.isExecutableFile(atPath: ma) else { return }
        savingsRunning = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let task = Process()
            task.executableURL = URL(fileURLWithPath: ma)
            task.arguments = ["savings", "--json"]
            let out = Pipe()
            task.standardOutput = out
            task.standardError = FileHandle.nullDevice
            var parsed: [String: Any] = [:]
            if (try? task.run()) != nil {
                let data = out.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                if task.terminationStatus == 0,
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    parsed = obj
                }
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.savingsRunning = false
                guard !parsed.isEmpty else { return }
                let tokens = parsed["tokens"] as? [String: Any] ?? [:]
                let space = parsed["space"] as? [String: Any] ?? [:]
                self.tokensTip = text(tokens["tooltip"])
                self.spaceTip = text(space["tooltip"])
                self.spaceSaved = text(space["label"])
            }
        }
    }

    @objc func startInRoom() {
        rememberMode("room")
        launch(paths.rec, ["start", "room"])
    }

    @objc func startHeadphones() {
        rememberMode("call")
        launch(paths.rec, ["start", "call"])
    }

    /// The shortcut starts whichever of these was pressed last, so one key
    /// works for both. Recording itself does not wait on the save.
    private func rememberMode(_ mode: String) {
        launch(paths.ma, ["setting", "record_mode", mode])
    }

    @objc func toggleRecording() {
        if read().recording {
            launch(paths.rec, ["stop"])
        }
    }

    private func primeRecordButton(_ button: NSButton, action: Selector) {
        button.target = self
        button.action = action
        button.bezelStyle = .rounded
        button.bezelColor = YarnColor.blue
        button.contentTintColor = .white
        button.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        button.setContentHuggingPriority(.required, for: .vertical)
        button.heightAnchor.constraint(equalToConstant: 32).isActive = true
    }

    @objc func processWaiting() { launch(paths.ma, ["process"]) }
    @objc func resume() { launch(paths.ma, ["resume"]) }

    @objc func ask() {
        AskChat.show(paths: paths)
    }

    private var briefsInFlight = 0

    @objc func brief() {
        guard FileManager.default.isExecutableFile(atPath: paths.ma) else {
            note("That action isn't available yet.")
            return
        }
        quips.hold(
            "Writing today's brief. It usually takes about a minute.",
            sticky: true,
            reason: "brief"
        )
        briefsInFlight += 1
        let ma = paths.ma
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let task = Process()
            task.executableURL = URL(fileURLWithPath: ma)
            task.arguments = ["brief", "--open", "--force"]
            var env = ProcessInfo.processInfo.environment
            env["MA_BIN"] = ma
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
            task.environment = env
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            do {
                try task.run()
                task.waitUntilExit()
            } catch {
                // The line still has to come down if the brief never started.
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.briefsInFlight = max(0, self.briefsInFlight - 1)
                if self.briefsInFlight == 0 {
                    self.quips.releaseSticky(reason: "brief")
                }
            }
        }
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
            }
        }
    }

    @objc func speakers() {
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
            }
        }
    }

    @objc func settings() {
        YarnSettings.show(paths: paths)
    }

    /// One fill for the whole meeting. A stage with no measured fraction
    /// creeps forward inside its own slice, and the number never goes back.
    func forwardPercent(
        meeting: String,
        before: Int,
        expected: Int,
        after: Int,
        phase: String,
        stage: Int,
        elapsed: Int,
        measured: Bool
    ) -> Int {
        if meeting != barMeeting {
            barMeeting = meeting
            barFloor = 0
        }
        let value: Int
        if before > 0 || expected > 0 || after > 0 {
            value = meetingPercent(
                before: before,
                expected: expected,
                after: after,
                elapsed: elapsed,
                stage: stage,
                indeterminate: !measured
            )
        } else {
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
            value = start + Int((Double(span) * inner).rounded())
        }
        barFloor = max(barFloor, min(99, value))
        return barFloor
    }

    var quips: QuipLine!

    func note(_ text: String) {
        quips.hold(text, sticky: false)
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

private func identifyWaitAlive(_ path: String) -> Bool {
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return false }
    let pidText = text.split(separator: "\n", omittingEmptySubsequences: false).first.map(String.init) ?? ""
    guard let pid = Int32(pidText), pid > 0 else { return false }
    return kill(pid, 0) == 0
}

/// Bands for the whole meeting. menu.sh copies these in overall_percent.
private func phaseBand(_ phase: String) -> (Int, Int) {
    // Fallback only, when the status file has no timings. The live bar uses
    // meetingPercent. Voice separation is most of the wait.
    switch phase {
    case "Preparing audio": return (0, 2)
    case "Transcribing": return (2, 16)
    case "Identifying speakers": return (16, 80)
    case "Recognising known speakers": return (80, 82)
    case "Saving transcript": return (82, 84)
    case "Indexing", "Indexing for search": return (84, 86)
    case "Cleaning transcript": return (86, 97)
    case "Writing summary": return (97, 100)
    default: return (2, 97)
    }
}

/// Seconds left in the current step. Same blend as pipeline.stage_seconds_left.
private func stageSecondsLeft(expected: Int, elapsed: Int, percent: Int, indeterminate: Bool) -> Int {
    let history: Int? = expected > 0 ? expected - elapsed : nil
    var progress: Int?
    if !indeterminate, percent >= 3, percent < 100, elapsed >= 10 {
        progress = elapsed * (100 - percent) / percent
    }
    if let progress, let history {
        let hist = max(0, history)
        return (percent * progress + (100 - percent) * hist) / 100
    }
    if let progress { return progress }
    if let history { return history }
    return -1
}

/// Whole-meeting percent. Same rule as pipeline.meeting_percent.
private func meetingPercent(
    before: Int, expected: Int, after: Int, elapsed: Int, stage: Int, indeterminate: Bool
) -> Int {
    if before <= 0 && expected <= 0 && after <= 0 { return 0 }
    var left = stageSecondsLeft(
        expected: expected, elapsed: elapsed, percent: stage, indeterminate: indeterminate
    )
    if left < 0 { left = 0 }
    let remain = max(0, left) + max(0, after)
    let spent = max(0, before) + max(0, elapsed)
    let total = spent + remain
    if total <= 0 { return 0 }
    var value = Int((Double(spent) * 100 / Double(total)).rounded())
    if remain > 0 { value = min(99, value) }
    return min(100, max(0, value))
}

/// Same words as pipeline._phase_estimate.
private func meetingEta(
    expected: Int, after: Int, learned: Bool, elapsed: Int, stage: Int, indeterminate: Bool
) -> String {
    var left = stageSecondsLeft(
        expected: expected, elapsed: elapsed, percent: stage, indeterminate: indeterminate
    )
    if left < 0 {
        if after <= 0 { return "" }
        left = 0
    }
    if left <= 0 && (indeterminate || stage < 3) {
        if after >= 60 { return "Taking longer than usual, then about \(span(after))" }
        if after > 0 { return "Taking longer than usual, then less than a minute" }
        return "Taking longer than usual, still working"
    }
    let total = max(0, left) + max(0, after)
    var text = total < 60 ? "Less than a minute left" : "About \(span(total)) left"
    if !learned { text += " (rough, Yarn is still learning this Mac)" }
    return text
}

/// Fast and Accurate are the names in the footer. The hover says which
/// model that is. The ids match meeting_archive/resources.py.
private func summaryChoice(_ raw: String) -> (label: String, tip: String) {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    let folded = trimmed.lowercased()
    if folded.isEmpty {
        let accurate = ProcessInfo.processInfo.physicalMemory > 10_737_418_240
        return accurate
            ? ("Accurate", "Accurate is qwen3.5:9b, running on this Mac.")
            : ("Fast", "Fast is qwen3.5:4b, running on this Mac.")
    }
    if folded.contains("9b") || folded == "accurate" {
        let name = folded.contains("qwen") ? trimmed : "qwen3.5:9b"
        return ("Accurate", "Accurate is \(name), running on this Mac.")
    }
    if folded.contains("4b") || folded == "fast" {
        let name = folded.contains("qwen") ? trimmed : "qwen3.5:4b"
        return ("Fast", "Fast is \(name), running on this Mac.")
    }
    return (trimmed, "Summaries use \(trimmed), running on this Mac.")
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
        return "Tidying punctuation and mishearings. The transcript is already saved."
    case "Indexing", "Indexing for search":
        return "Filing this meeting so Ask Yarn can search it."
    case "Writing summary":
        return "Writing the summary on this Mac."
    default:
        return ""
    }
}

func stack(_ views: [NSView], spacing: CGFloat, horizontal: Bool = false) -> NSStackView {
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

func card(_ content: NSView, owner: AnyObject) -> NSView {
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

func label(_ text: String, size: CGFloat, weight: NSFont.Weight, color: NSColor, mono: Bool = false) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    style(field, size: size, weight: weight, color: color, mono: mono)
    return field
}

func style(_ field: NSTextField, size: CGFloat, weight: NSFont.Weight, color: NSColor, mono: Bool = false) {
    field.font = mono
        ? NSFont.monospacedSystemFont(ofSize: size, weight: weight)
        : NSFont.systemFont(ofSize: size, weight: weight)
    field.textColor = color
    field.maximumNumberOfLines = 2
    field.lineBreakMode = .byWordWrapping
}

func link(_ title: String, _ action: Selector, _ target: AnyObject) -> NSButton {
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

func json(_ path: String) -> [String: Any] {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return [:]
    }
    return obj
}

func text(_ value: Any?) -> String {
    if let value = value as? String { return value }
    if let value = value as? NSNumber { return value.stringValue }
    return ""
}

func integer(_ value: Any?) -> Int? {
    if let value = value as? Int { return value }
    if let value = value as? NSNumber { return value.intValue }
    if let value = value as? String { return Int(value) }
    return nil
}

func boolean(_ value: Any?) -> Bool {
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

/// 648,255 is "648k". A million is "1.2M", ten million is "12M".
private func formatTokens(_ count: Int) -> String {
    let n = Double(max(0, count))
    if n >= 1_000_000_000 { return tokenUnit(n / 1_000_000_000, "B") }
    if n >= 1_000_000 { return tokenUnit(n / 1_000_000, "M") }
    if n >= 1_000 { return tokenUnit(n / 1_000, "k") }
    return "\(count)"
}

private func tokenUnit(_ value: Double, _ suffix: String) -> String {
    if value >= 9.95 {
        let whole = Int(value.rounded())
        if whole >= 1000 {
            let next = suffix == "k" ? "M" : "B"
            return tokenUnit(value / 1000, next)
        }
        return "\(whole)\(suffix)"
    }
    return String(format: "%.1f\(suffix)", value)
}

func markImage() -> NSImage? {
    guard let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png") else { return nil }
    return NSImage(contentsOf: url)
}

/// The line under the buttons. It cycles through asides, and a real status
/// borrows it until that status is done.
final class QuipLine {
    static let lines = [
        "How good this is free!",
        "Another subscription avoided.",
        "Your meetings. Not our business.",
        "Something witty about SaaS. I\u{2019}m tired.",
        "Your data isn\u{2019}t our business model.",
        "Small brain. Big ambitions.",
        "Hi Mum! Jokes, she doesn\u{2019}t use a computer.",
        "Powered by your Mac. Not your wallet.",
        "Your IP. Not theirs. You know who.",
        "We can\u{2019}t sell your data. We don\u{2019}t have it.",
    ]

    private let label: NSTextField
    private var index: Int
    private var deck: [Int]
    private var quip: String
    private var timer: Timer?
    private var generation = 0
    private var holdToken = 0
    private enum Mode { case cycling, sticky, temporary }
    private var mode: Mode = .cycling
    private var stickyReason = ""

    init(label: NSTextField) {
        self.label = label
        let start = Int.random(in: 0..<Self.lines.count)
        index = start
        quip = Self.lines[start]
        deck = Array(Self.lines.indices).filter { $0 != start }.shuffled()
        label.wantsLayer = true
        label.stringValue = quip
        label.alphaValue = 1
        label.isHidden = false
        label.textColor = YarnColor.slate
        schedule()
    }

    func hold(_ text: String, sticky: Bool, reason: String = "") {
        if reason == "ollama", stickyReason == "brief" { return }
        if sticky, mode == .sticky, stickyReason == reason, label.stringValue == text { return }
        mode = sticky ? .sticky : .temporary
        stickyReason = sticky ? reason : ""
        holdToken += 1
        let token = holdToken
        timer?.invalidate()
        timer = nil
        label.textColor = YarnColor.slate
        show(text, animated: label.stringValue != text)
        if sticky { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [weak self] in
            guard let self, token == self.holdToken, self.mode == .temporary else { return }
            self.mode = .cycling
            self.label.textColor = YarnColor.slate
            self.show(self.quip, animated: self.label.stringValue != self.quip)
            self.schedule()
        }
    }

    func releaseSticky(reason: String = "") {
        guard mode == .sticky else { return }
        if !reason.isEmpty, stickyReason != reason { return }
        mode = .cycling
        stickyReason = ""
        holdToken += 1
        label.textColor = YarnColor.slate
        show(quip, animated: label.stringValue != quip)
        schedule()
    }

    private func schedule() {
        timer?.invalidate()
        let wait = Double.random(in: 12...16)
        timer = Timer.scheduledTimer(withTimeInterval: wait, repeats: false) { [weak self] _ in
            self?.advance()
        }
    }

    private func advance() {
        guard mode == .cycling else { return }
        if deck.isEmpty {
            deck = Array(Self.lines.indices).shuffled()
            if deck.count > 1, deck[0] == index {
                deck.swapAt(0, 1)
            }
        }
        index = deck.removeFirst()
        quip = Self.lines[index]
        label.textColor = YarnColor.slate
        show(quip, animated: true)
        schedule()
    }

    private func show(_ text: String, animated: Bool) {
        generation += 1
        let token = generation
        label.isHidden = false
        if !animated {
            label.stringValue = text
            label.alphaValue = 1
            return
        }
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.35
            label.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self, token == self.generation else { return }
            self.label.stringValue = text
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.45
                self.label.animator().alphaValue = 1
            }
        })
    }
}

@main
enum YarnDock {
    static func main() {
        let app = NSApplication.shared
        let delegate = Panel()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        YarnAppMenu.install(settings: (delegate, #selector(Panel.settings)))
        app.run()
    }
}
