// Yarn Setup. The first window is a short welcome, then the real install,
// then a short "you're done". How to record, background processing, audio
// retention and the daily brief are explained when Yarn itself opens.

import AppKit
import Darwin

struct Step: Equatable {
    let id: String
    var state: String = "pending" // pending | running | done | failed
}

// Shown in the order the installer actually runs them. The labels are the
// ones people see. The ids are the worker's, so a tick means that work finished.
private let faceGroups: [(title: String, ids: [String])] = [
    ("Checking your Mac", ["prereqs"]),
    ("Installing required components", ["venv", "deps", "tools"]),
    ("Setting up your meeting archive", ["config"]),
    ("Downloading local AI models", ["whisper", "speakers", "ollama"]),
    ("Adding Yarn to your menu bar", ["menubar"]),
]

private let modelIDs: Set<String> = ["whisper", "speakers", "ollama"]

final class Controller: NSObject, NSWindowDelegate {
    let repo: String
    var python: String
    var steps: [Step] = []
    var worker: Process?
    var finished = false
    var downloadPercent: Int?
    var stray = ""

    let window: NSWindow
    let stack = NSStackView()
    let logo = NSImageView()
    let welcomeBox = NSStackView()
    let progressBox = NSStackView()
    let doneBox = NSStackView()
    let heading = NSTextField(labelWithString: "Installing Yarn")
    let personality = NSTextField(wrappingLabelWithString: "")
    let detail = NSTextField(wrappingLabelWithString: "")
    private let installButton = SetupButton(title: "Install Yarn")
    private let openButton = SetupButton(title: "Open Yarn")
    let closeButton = NSButton(title: "Close", target: nil, action: nil)
    let doneNote = NSTextField(wrappingLabelWithString: "Yarn is installed, but the app was not where Setup expected it. Open Yarn from your home Applications folder.")
    private var rows: [FaceRow] = []
    private let activityScroll = NSScrollView()
    private let activityText = NSTextView()
    private var activityLines: [String] = []

    init(repo: String, python: String) {
        self.repo = repo
        self.python = python
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 640),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "Yarn Setup"
        window.delegate = self
        window.isReleasedWhenClosed = false
        // The mark is the blue yarn on a clear background. The window stays
        // light so that mark sits on white in dark mode as well.
        window.appearance = NSAppearance(named: .aqua)
        window.backgroundColor = .white
        build()
    }

    func start() {
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func build() {
        let ink = yarnColor(0x19293D)
        let navy = yarnColor(0x0D244B)
        let slate = yarnColor(0x617188)
        let blue = yarnColor(0x167DFF)

        logo.image = loadMark()
        logo.imageScaling = .scaleProportionallyUpOrDown
        logo.translatesAutoresizingMaskIntoConstraints = false
        logo.widthAnchor.constraint(equalToConstant: 132).isActive = true
        logo.heightAnchor.constraint(equalToConstant: 65).isActive = true

        welcomeBox.orientation = .vertical
        welcomeBox.alignment = .leading
        welcomeBox.spacing = 8
        welcomeBox.translatesAutoresizingMaskIntoConstraints = false

        welcomeBox.addArrangedSubview(label("Welcome to Yarn", size: 26, weight: .semibold, color: navy))
        let tag = label(
            "Your meetings. Your memories. Your computer.",
            size: 15, weight: .medium, color: blue
        )
        welcomeBox.addArrangedSubview(tag)
        welcomeBox.setCustomSpacing(12, after: tag)
        welcomeBox.addArrangedSubview(label(
            "Yarn records your meetings, figures out who said what, and turns conversations into searchable notes and a daily brief.",
            size: 13, weight: .regular, color: ink
        ))
        welcomeBox.addArrangedSubview(label(
            "No subscriptions. No cloud AI processing. Your conversations stay on your Mac.",
            size: 13, weight: .regular, color: ink
        ))
        welcomeBox.setCustomSpacing(14, after: welcomeBox.arrangedSubviews.last!)
        welcomeBox.addArrangedSubview(label(
            "Small brain. Surprisingly useful. (Like its creator.)",
            size: 15, weight: .semibold, color: navy
        ))
        welcomeBox.addArrangedSubview(label(
            "Yarn uses free, local AI models instead of sending your meetings to someone else's computer.",
            size: 13, weight: .regular, color: ink
        ))
        welcomeBox.addArrangedSubview(label(
            "We'll get everything ready for you.",
            size: 13, weight: .regular, color: ink
        ))
        welcomeBox.setCustomSpacing(12, after: welcomeBox.arrangedSubviews.last!)
        welcomeBox.addArrangedSubview(label(
            "What we're setting up:",
            size: 13, weight: .semibold, color: navy
        ))
        addSetupLine(
            "1",
            "Yarn. Your meeting recorder and searchable archive.",
            blue: blue, ink: ink
        )
        addSetupLine(
            "2",
            "Local AI. Free models for transcription, speaker identification and summaries.",
            blue: blue, ink: ink
        )
        addSetupLine(
            "3",
            "Your menu bar. The little Yarn icon, ready when you need it.",
            blue: blue, ink: ink
        )
        welcomeBox.setCustomSpacing(12, after: welcomeBox.arrangedSubviews.last!)
        welcomeBox.addArrangedSubview(label(
            "Some components need downloading once. After that, Yarn processes your meetings locally.",
            size: 12, weight: .regular, color: slate
        ))

        installButton.target = self
        installButton.action = #selector(beginInstall)
        installButton.keyEquivalent = "\r"
        installButton.translatesAutoresizingMaskIntoConstraints = false
        welcomeBox.setCustomSpacing(16, after: welcomeBox.arrangedSubviews.last!)
        welcomeBox.addArrangedSubview(installButton)
        installButton.widthAnchor.constraint(equalToConstant: 148).isActive = true
        installButton.heightAnchor.constraint(equalToConstant: 36).isActive = true

        progressBox.orientation = .vertical
        progressBox.alignment = .leading
        progressBox.spacing = 10
        progressBox.translatesAutoresizingMaskIntoConstraints = false
        progressBox.isHidden = true

        heading.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        heading.textColor = navy
        personality.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        personality.textColor = blue
        personality.maximumNumberOfLines = 3
        personality.preferredMaxLayoutWidth = textWidth
        detail.font = NSFont.systemFont(ofSize: 13)
        detail.textColor = yarnColor(0xB42318)
        detail.maximumNumberOfLines = 6
        detail.preferredMaxLayoutWidth = textWidth
        detail.isHidden = true

        progressBox.addArrangedSubview(heading)
        progressBox.addArrangedSubview(personality)
        for group in faceGroups {
            let row = FaceRow(title: group.title)
            rows.append(row)
            progressBox.addArrangedSubview(row.view)
        }
        progressBox.addArrangedSubview(detail)
        buildActivity()
        progressBox.addArrangedSubview(activityScroll)
        for view in progressBox.arrangedSubviews {
            view.setContentHuggingPriority(.defaultHigh, for: .vertical)
        }
        activityScroll.setContentHuggingPriority(.init(1), for: .vertical)

        closeButton.target = self
        closeButton.action = #selector(cancelRun)
        closeButton.keyEquivalent = "\u{1b}"
        closeButton.isHidden = true
        progressBox.addArrangedSubview(closeButton)

        doneBox.orientation = .vertical
        doneBox.alignment = .leading
        doneBox.spacing = 10
        doneBox.translatesAutoresizingMaskIntoConstraints = false
        doneBox.isHidden = true
        doneBox.addArrangedSubview(label("You\u{2019}re almost there.", size: 26, weight: .semibold, color: navy))
        doneBox.addArrangedSubview(label(
            "One setting and Yarn is ready to remember the things you won\u{2019}t.",
            size: 15, weight: .regular, color: ink
        ))
        doneBox.addArrangedSubview(label(
            "If your Mac asks, click Allow. This only lets Yarn hear calls while you\u{2019}re wearing headphones, and it doesn\u{2019}t record your screen. No prompt? Under Privacy & Security, \(screenRecordingRow), click +, choose Yarn, and turn it on. Yarn is in the Applications folder in your home folder. Yarn runs only on this Mac, and everything it records is yours. Don\u{2019}t need this? No hard feelings, just continue.",
            size: 13, weight: .regular, color: ink
        ))
        openButton.target = self
        openButton.action = #selector(openYarn)
        openButton.keyEquivalent = "\r"
        openButton.translatesAutoresizingMaskIntoConstraints = false
        doneBox.setCustomSpacing(16, after: doneBox.arrangedSubviews.last!)
        doneBox.addArrangedSubview(openButton)
        openButton.widthAnchor.constraint(equalToConstant: 140).isActive = true
        openButton.heightAnchor.constraint(equalToConstant: 36).isActive = true
        doneNote.font = NSFont.systemFont(ofSize: 13)
        doneNote.textColor = yarnColor(0xB42318)
        doneNote.maximumNumberOfLines = 0
        doneNote.preferredMaxLayoutWidth = textWidth
        doneNote.isHidden = true

        let logoRow = NSView()
        logoRow.translatesAutoresizingMaskIntoConstraints = false
        logoRow.addSubview(logo)
        NSLayoutConstraint.activate([
            logo.topAnchor.constraint(equalTo: logoRow.topAnchor),
            logo.bottomAnchor.constraint(equalTo: logoRow.bottomAnchor),
            logo.centerXAnchor.constraint(equalTo: logoRow.centerXAnchor),
            logoRow.widthAnchor.constraint(equalToConstant: textWidth),
        ])

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.edgeInsets = NSEdgeInsets(top: 22, left: 28, bottom: 22, right: 28)
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(logoRow)
        stack.addArrangedSubview(welcomeBox)
        stack.addArrangedSubview(progressBox)
        stack.addArrangedSubview(doneBox)
        welcomeBox.widthAnchor.constraint(equalToConstant: textWidth).isActive = true
        progressBox.widthAnchor.constraint(equalToConstant: textWidth).isActive = true
        doneBox.widthAnchor.constraint(equalToConstant: textWidth).isActive = true

        let content = NSView()
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor.white.cgColor
        window.contentView = content
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])
        stack.layoutSubtreeIfNeeded()
        let height = ceil(stack.fittingSize.height)
        window.setContentSize(NSSize(width: 520, height: height))
    }

    private var textWidth: CGFloat { 464 }

    private var screenRecordingRow: String {
        if #available(macOS 15, *) {
            return "Screen & System Audio Recording"
        }
        return "Screen Recording"
    }

    private func label(_ text: String, size: CGFloat, weight: NSFont.Weight, color: NSColor) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = NSFont.systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.maximumNumberOfLines = 0
        field.preferredMaxLayoutWidth = textWidth
        return field
    }

    private func addSetupLine(_ number: String, _ text: String, blue: NSColor, ink: NSColor) {
        let n = NSTextField(labelWithString: number)
        n.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        n.textColor = blue
        n.translatesAutoresizingMaskIntoConstraints = false
        n.widthAnchor.constraint(equalToConstant: 16).isActive = true
        let words = NSTextField(wrappingLabelWithString: text)
        words.font = NSFont.systemFont(ofSize: 13)
        words.textColor = ink
        words.maximumNumberOfLines = 0
        words.preferredMaxLayoutWidth = textWidth - 24
        let row = NSStackView(views: [n, words])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 8
        welcomeBox.addArrangedSubview(row)
    }

    private func loadMark() -> NSImage? {
        if let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png"),
           let image = NSImage(contentsOf: url) {
            return image
        }
        let path = (repo as NSString).appendingPathComponent("meeting_archive/assets/yarn-mark.png")
        return NSImage(contentsOfFile: path)
    }

    private func showProgress() {
        welcomeBox.isHidden = true
        doneBox.isHidden = true
        progressBox.isHidden = false
        heading.stringValue = "Installing Yarn"
        personality.stringValue = ""
        detail.stringValue = ""
        detail.isHidden = true
        closeButton.isHidden = true
        installButton.keyEquivalent = ""
        window.title = "Yarn Setup"
    }

    private func showDone() {
        welcomeBox.isHidden = true
        progressBox.isHidden = true
        doneBox.isHidden = false
        openButton.keyEquivalent = "\r"
        window.title = "Yarn Setup"
    }

    private func showFailure(_ message: String) {
        showProgress()
        heading.stringValue = "Yarn couldn't finish"
        personality.stringValue = ""
        detail.stringValue = message
        detail.isHidden = message.isEmpty
        closeButton.isHidden = false
        closeButton.title = "Close"
        finished = true
    }

    @objc private func beginInstall() {
        showProgress()
        heading.stringValue = "Checking your Mac"
        personality.stringValue = "Looking at what's already on this Mac."
        noteActivity("Checking Python, memory, and disk space.")
        resolvePythonThenWork()
    }

    // The window is already up. Finding or installing Python happens here so
    // a fresh Mac never dies in Terminal before anyone can see a check.
    private var pythonNotes = ""

    private func resolvePythonThenWork() {
        let checker = (repo as NSString).appendingPathComponent("bin/yarn-python")
        guard FileManager.default.isExecutableFile(atPath: checker) else {
            launchWorker()
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [checker]
        var env = ProcessInfo.processInfo.environment
        let extra = "/Library/Developer/CommandLineTools/usr/bin:/opt/homebrew/bin:/usr/local/bin"
        env["PATH"] = extra + ":" + (env["PATH"] ?? "")
        process.environment = env
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                guard let self else { return }
                self.pythonNotes += text
                for line in text.split(whereSeparator: \.isNewline) {
                    let shown = String(line).trimmingCharacters(in: .whitespaces)
                    if !shown.isEmpty { self.noteActivity(shown) }
                }
            }
        }
        process.terminationHandler = { [weak self] proc in
            let data = out.fileHandleForReading.readDataToEndOfFile()
            let path = (String(data: data, encoding: .utf8) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                guard let self else { return }
                err.fileHandleForReading.readabilityHandler = nil
                if proc.terminationStatus == 0, path.hasPrefix("/") {
                    self.python = path
                    self.noteActivity("Python is ready.")
                    self.launchWorker()
                } else {
                    let message = self.pythonNotes.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.showFailure(
                        message.isEmpty
                            ? "Yarn needs Python 3.11 or newer, and installing it did not work."
                            : message
                    )
                }
            }
        }
        do {
            try process.run()
        } catch {
            showFailure("Could not start the checks. \(error.localizedDescription)")
        }
    }

    private func launchWorker() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [
            "-m", "meeting_archive.setup",
            "--repo", repo,
            "--pull", pullArgument(),
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
            showFailure("Could not start setup. \(error.localizedDescription)")
        }
    }

    // Same choice the old checkboxes made, without putting it on the welcome.
    // A Mac with 10 GB or less cannot run the larger summary model.
    private func pullArgument() -> String {
        let ram = memoryGB()
        if ram > 0 && ram <= 10 { return "fast" }
        return "fast,accurate"
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
            if !trimmed.isEmpty { stray = trimmed }
            return
        }
        switch type {
        case "hello":
            break
        case "start":
            let id = obj["id"] as? String ?? ""
            set(id, state: "running")
            downloadPercent = nil
        case "log":
            if let text = obj["text"] as? String {
                noteActivity(text)
            }
        case "progress":
            if let id = obj["id"] as? String, modelIDs.contains(id),
               let percent = obj["percent"] as? Int {
                downloadPercent = min(100, max(0, percent))
            }
        case "done":
            let id = obj["id"] as? String ?? ""
            let ok = obj["ok"] as? Bool ?? false
            set(id, state: ok ? "done" : "failed")
            if !ok {
                showFailure(obj["message"] as? String ?? "Something needs attention.")
            }
        case "finish":
            finished = true
            let ok = obj["ok"] as? Bool ?? false
            if ok {
                showDone()
            } else if detail.isHidden {
                showFailure(obj["message"] as? String ?? "Something needs attention.")
            }
        default:
            break
        }
        refreshRows()
    }

    private func buildActivity() {
        activityText.isEditable = false
        activityText.isSelectable = true
        activityText.drawsBackground = false
        activityText.isRichText = false
        activityText.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        activityText.textColor = yarnColor(0x8A97A8)
        activityText.textContainerInset = NSSize(width: 0, height: 8)
        activityText.isVerticallyResizable = true
        activityText.isHorizontallyResizable = false
        activityText.autoresizingMask = [.width]
        activityText.textContainer?.widthTracksTextView = true
        activityText.textContainer?.lineFragmentPadding = 2
        activityScroll.documentView = activityText
        activityScroll.drawsBackground = false
        activityScroll.borderType = .noBorder
        activityScroll.hasVerticalScroller = true
        activityScroll.autohidesScrollers = true
        activityScroll.translatesAutoresizingMaskIntoConstraints = false
        activityScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        activityScroll.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        activityScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 96).isActive = true
    }

    private func noteActivity(_ line: String) {
        let clean = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        activityLines.append(clean)
        if activityLines.count > 80 {
            activityLines.removeFirst(activityLines.count - 80)
        }
        let shown = activityLines.joined(separator: "\n")
        activityText.textStorage?.setAttributedString(NSAttributedString(
            string: shown,
            attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular),
                .foregroundColor: yarnColor(0x8A97A8),
            ]
        ))
        activityText.scrollToEndOfDocument(nil)
    }

    private func set(_ id: String, state: String) {
        if let i = steps.firstIndex(where: { $0.id == id }) {
            steps[i].state = state
        } else if !id.isEmpty {
            steps.append(Step(id: id, state: state))
        }
    }

    private func refreshRows() {
        let modelsRunning = faceGroups.contains { group in
            group.ids.contains(where: { modelIDs.contains($0) }) && groupState(group.ids) == "running"
        }
        if !finished {
            personality.stringValue = modelsRunning
                ? "Downloading a small brain. Will take a minute. When was the last time you called your Mum?"
                : ""
        }
        for (index, group) in faceGroups.enumerated() {
            let state = groupState(group.ids)
            let percent = (state == "running" && group.ids.contains(where: { modelIDs.contains($0) }))
                ? downloadPercent
                : nil
            rows[index].apply(state: state, percent: percent)
        }
    }

    private func groupState(_ ids: [String]) -> String {
        let states = ids.compactMap { id in steps.first { $0.id == id }?.state }
        if states.contains("failed") { return "failed" }
        if states.contains("running") { return "running" }
        if states.count == ids.count && states.allSatisfy({ $0 == "done" }) { return "done" }
        if states.contains("done") { return "running" }
        return "pending"
    }

    private func workerFinished(code: Int32) {
        worker = nil
        if !finished {
            if code == 0 {
                showDone()
            } else {
                let message = !detail.stringValue.isEmpty
                    ? detail.stringValue
                    : (stray.isEmpty ? "Setup stopped before it finished." : stray)
                showFailure(message)
            }
        }
        finished = true
    }

    @objc private func openYarn() {
        // Leave the how-to welcome for Yarn itself. Setup does not mark it seen.
        let app = NSHomeDirectory() + "/Applications/Yarn.app"
        guard FileManager.default.fileExists(atPath: app) else {
            doneBox.isHidden = false
            openButton.isHidden = true
            if doneNote.superview == nil {
                doneBox.addArrangedSubview(doneNote)
            }
            doneNote.isHidden = false
            return
        }
        // Screen recording is read when Yarn opens. A copy left running from
        // Setup started before the switch was turned on, so quit it first.
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.meetingarchive.yarn")
        for process in running {
            process.terminate()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            NSWorkspace.shared.open(URL(fileURLWithPath: app))
            NSApp.terminate(nil)
        }
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

private final class FaceRow {
    let view = NSStackView()
    private let mark = NSImageView()
    private let spinner = NSProgressIndicator()
    private let title = NSTextField(labelWithString: "")
    private let percent = NSTextField(labelWithString: "")

    init(title text: String) {
        title.stringValue = text
        title.font = NSFont.systemFont(ofSize: 13)
        title.textColor = yarnColor(0x617188)
        title.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        title.lineBreakMode = .byTruncatingTail
        percent.font = NSFont.systemFont(ofSize: 12)
        percent.textColor = yarnColor(0x167DFF)
        percent.alignment = .right
        percent.setContentHuggingPriority(.required, for: .horizontal)
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.translatesAutoresizingMaskIntoConstraints = false
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.isHidden = true
        let slot = NSView()
        slot.translatesAutoresizingMaskIntoConstraints = false
        slot.addSubview(mark)
        slot.addSubview(spinner)
        NSLayoutConstraint.activate([
            slot.widthAnchor.constraint(equalToConstant: 16),
            slot.heightAnchor.constraint(equalToConstant: 16),
            mark.leadingAnchor.constraint(equalTo: slot.leadingAnchor),
            mark.trailingAnchor.constraint(equalTo: slot.trailingAnchor),
            mark.topAnchor.constraint(equalTo: slot.topAnchor),
            mark.bottomAnchor.constraint(equalTo: slot.bottomAnchor),
            spinner.leadingAnchor.constraint(equalTo: slot.leadingAnchor),
            spinner.trailingAnchor.constraint(equalTo: slot.trailingAnchor),
            spinner.topAnchor.constraint(equalTo: slot.topAnchor),
            spinner.bottomAnchor.constraint(equalTo: slot.bottomAnchor),
        ])
        view.orientation = .horizontal
        view.alignment = .centerY
        view.spacing = 8
        view.translatesAutoresizingMaskIntoConstraints = false
        let spacer = NSView()
        spacer.setContentHuggingPriority(.init(1), for: .horizontal)
        view.addArrangedSubview(slot)
        view.addArrangedSubview(title)
        view.addArrangedSubview(spacer)
        view.addArrangedSubview(percent)
        view.widthAnchor.constraint(equalToConstant: 464).isActive = true
        apply(state: "pending", percent: nil)
    }

    func apply(state: String, percent shown: Int?) {
        let running = state == "running"
        spinner.isHidden = !running
        mark.isHidden = running
        if running {
            spinner.startAnimation(nil)
        } else {
            spinner.stopAnimation(nil)
        }
        mark.image = symbol(state)
        mark.contentTintColor = tint(state)
        title.textColor = state == "pending" ? yarnColor(0x617188) : yarnColor(0x19293D)
        title.font = NSFont.systemFont(ofSize: 13, weight: running ? .semibold : .regular)
        if let shown, running {
            percent.stringValue = "\(shown)%"
        } else {
            percent.stringValue = ""
        }
    }

    private func symbol(_ state: String) -> NSImage? {
        let name: String
        switch state {
        case "done": name = "checkmark.circle.fill"
        case "failed": name = "xmark.circle.fill"
        default: name = "circle"
        }
        return NSImage(systemSymbolName: name, accessibilityDescription: nil)
    }

    private func tint(_ state: String) -> NSColor {
        switch state {
        case "done": return yarnColor(0x167DFF)
        case "failed": return yarnColor(0xB42318)
        default: return yarnColor(0xC5D0DC)
        }
    }
}

private final class SetupButton: NSButton {
    convenience init(title: String) {
        self.init(frame: .zero)
        self.title = title
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 8
    }

    override func draw(_ dirtyRect: NSRect) {
        yarnColor(0x167DFF).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
        let shown = title as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 14, weight: .semibold),
            .foregroundColor: NSColor.white,
        ]
        let size = shown.size(withAttributes: attrs)
        shown.draw(
            in: NSRect(
                x: (bounds.width - size.width) / 2,
                y: (bounds.height - size.height) / 2 - 1,
                width: size.width,
                height: size.height
            ),
            withAttributes: attrs
        )
    }
}

private func yarnColor(_ hex: UInt32) -> NSColor {
    NSColor(
        srgbRed: CGFloat((hex >> 16) & 0xff) / 255,
        green: CGFloat((hex >> 8) & 0xff) / 255,
        blue: CGFloat(hex & 0xff) / 255,
        alpha: 1
    )
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: Controller?

    func applicationDidFinishLaunching(_ notification: Notification) {
        var args = Array(CommandLine.arguments.dropFirst())
        // The download is this app. macOS may open it from a temporary
        // folder, so the program has to travel inside the app. A checkout
        // passes --repo and is left alone.
        var repo = programDirectory() ?? FileManager.default.currentDirectoryPath
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

func programDirectory() -> String? {
    guard let resources = Bundle.main.resourceURL else { return nil }
    let bundled = resources.appendingPathComponent("yarn")
    let marker = bundled.appendingPathComponent("bin/yarn-setup").path
    if FileManager.default.isExecutableFile(atPath: marker) {
        return bundled.path
    }
    return nil
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
