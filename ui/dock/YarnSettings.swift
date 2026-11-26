// Settings, in the same window style as Ask Yarn. This is the full control
// panel: everything the menu bar offers, plus the options that used to need
// a text editor. Every change goes through `ma setting`, the same command
// the menu bar runs, so the menu, this window and the command line read and
// write one file and cannot disagree.

import AppKit
import UniformTypeIdentifiers

final class YarnSettings: NSObject, NSWindowDelegate {
    private let paths: Paths
    private let window: NSWindow
    private let language = SettingChoices()
    private let floor = SettingChoices()
    private let hotkey = SettingChoices()
    private let profile = SettingChoices()
    private let evening = SettingChoices()
    private let nameField = NSTextField(string: "")
    private let identify = SettingSwitch(
        title: "Ask me to name voices",
        caption: "After a meeting is transcribed, Yarn asks who each separate voice was. Closing the window changes nothing."
    )
    private let suggest = SettingSwitch(
        title: "Suggest who was there",
        caption: "If you left the attendees empty, Yarn offers the names it heard. It never fills them in for you."
    )
    private let cleanup = SettingSwitch(
        title: "Tidy the transcript",
        caption: "The local model fixes punctuation and obvious mishearings, and every line is checked against what was said. The original wording is kept. The transcript is saved first, so this does not hold it up."
    )
    private let pressure = SettingSwitch(
        title: "Wait when the Mac is busy",
        caption: "While you are recording and memory is tight, transcription and summaries wait. The recording always comes first."
    )
    private let pages = SettingChoices()
    private let external = SettingSwitch(
        title: "Connect external AI apps",
        caption: "Lets Claude, Cursor or ChatGPT search your meetings. Off, Yarn is local from end to end."
    )
    private var externalBody: NSView?
    private var externalOn = false
    private var savedPages = "never"
    private static let retentionChoices: [(String, String, Bool)] = [
        ("1_week", "1 week", true),
        ("2_weeks", "2 weeks", true),
        ("1_month", "1 month", true),
        ("3_months", "3 months", true),
        ("6_months", "6 months", true),
        ("1_year", "1 year", true),
        ("never", "Never", true),
    ]
    private static let retentionIDs = retentionChoices.map { $0.0 }
    private let retention = SettingChoices()
    private let process = SettingChoices()
    private let summary = SettingChoices()
    private let onMac = SettingChoices()
    private var onMacModels: [String] = []
    private var summaryPick = "fast"
    private let recognise = SettingSwitch(
        title: "Recognise known speakers",
        caption: "A voice Yarn already knows can keep its name."
    )
    private let brief = SettingSwitch(
        title: "Open brief at login",
        caption: "Opens today\u{2019}s brief when you log in."
    )
    private let briefTip = NSTextField(labelWithString: "Tip: try Open brief at login after a few mornings")
    private let thread = ThreadEditor()
    private let glossary = ThreadEditor()
    private let status = NSTextField(labelWithString: "")
    private var writeGeneration = 0
    private var writing = false
    private let writer = DispatchQueue(label: "yarn.settings.writer")
    private let canAccurate: Bool
    private static var current: YarnSettings?

    static func show(paths: Paths) {
        if current == nil {
            current = YarnSettings(paths: paths)
        }
        current?.reload()
        current?.loadOnMac()
        current?.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    init(paths: Paths) {
        self.paths = paths
        canAccurate = Self.machineCanRunAccurate()
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 680),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "Settings"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = YarnColor.canvas
        window.minSize = NSSize(width: 420, height: 480)
        window.isReleasedWhenClosed = false
        window.delegate = self
        build()
        if !window.setFrameUsingName("YarnSettings") { window.center() }
        window.setFrameAutosaveName("YarnSettings")
    }

    func windowDidBecomeKey(_ notification: Notification) {
        if !writing { reload() }
    }

    func windowDidResignKey(_ notification: Notification) {
        thread.commit()
        glossary.commit()
    }

    private func build() {
        let content = NSView()
        window.contentView = content

        let mark = NSImageView()
        mark.image = markImage()
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Settings")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        title.textColor = YarnColor.navy
        let subtitle = NSTextField(wrappingLabelWithString: "Make Yarn your own.")
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = YarnColor.slate
        let titles = NSStackView(views: [title, subtitle])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 2
        let header = NSStackView(views: [mark, titles])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 10
        header.translatesAutoresizingMaskIntoConstraints = false

        language.onPick = { [weak self] id in
            self?.language.select(id)
            self?.write("meeting_language", id)
        }
        hotkey.onPick = { [weak self] id in
            self?.hotkey.select(id)
            self?.write("record_hotkey", id)
        }
        floor.onPick = { [weak self] id in
            self?.floor.select(id)
            self?.write("min_participant_s", id)
        }
        profile.onPick = { [weak self] id in
            self?.profile.select(id)
            self?.write("summary_profile", id)
        }
        evening.onPick = { [weak self] id in
            self?.evening.select(id)
            self?.write("evening_hour", id)
        }
        identify.onChange = { [weak self] on in
            self?.write("identify_popup", on ? "on" : "off")
        }
        suggest.onChange = { [weak self] on in
            self?.write("suggest_names_popup", on ? "on" : "off")
        }
        cleanup.onChange = { [weak self] on in
            self?.write("cleanup_enabled", on ? "on" : "off")
        }
        pressure.onChange = { [weak self] on in
            self?.write("pause_on_pressure", on ? "on" : "off")
        }
        pages.onPick = { [weak self] id in
            self?.pagesChanged(id)
        }
        external.onChange = { [weak self] on in
            self?.externalChanged(on)
        }
        nameField.placeholderString = "Your name"
        nameField.font = .systemFont(ofSize: 13)
        nameField.target = self
        nameField.action = #selector(saveName)
        nameField.setAccessibilityLabel("Your name on headphone recordings")
        nameField.translatesAutoresizingMaskIntoConstraints = false
        retention.onPick = { [weak self] id in
            self?.retention.select(id)
            self?.write("audio_retention", id)
        }
        process.onPick = { [weak self] id in
            self?.process.select(id)
            self?.write("summarize_when", id)
        }
        summary.onPick = { [weak self] id in
            guard let self else { return }
            self.summaryPick = id
            self.summary.select(id)
            self.onMac.select("builtin")
            self.write("summary_model", id)
        }
        onMac.onPick = { [weak self] id in
            guard let self else { return }
            self.onMac.select(id)
            if id == "builtin" {
                self.write("summary_model", self.summaryPick)
            } else {
                self.write("summary_model", id)
            }
        }
        recognise.onChange = { [weak self] on in
            self?.write("recognise_known", on ? "on" : "off")
        }
        brief.onChange = { [weak self] on in
            self?.write("brief_on_login", on ? "on" : "off")
        }
        thread.onCommit = { [weak self] text in
            self?.write("thread", text)
        }
        glossary.placeholderText = "Zora = Zuora"
        glossary.limit = 4000
        glossary.onCommit = { [weak self] text in
            self?.write("glossary", text)
        }

        let accurate = canAccurate
            ? "Accurate (9B)"
            : "Accurate (9B) needs about 16 GB"
        language.show([
            ("en", "English", true),
            ("auto", "Decide from the audio", true),
            ("ja", "Japanese", true),
            ("zh", "Chinese", true),
            ("es", "Spanish", true),
            ("fr", "French", true),
            ("de", "German", true),
            ("ko", "Korean", true),
        ], selected: "en")
        hotkey.show([
            ("off", "Off", true),
            ("on", "On", true),
        ], selected: "off")
        floor.show([
            ("2", "2 seconds", true),
            ("3", "3 seconds", true),
            ("5", "5 seconds", true),
            ("0", "Off: list every voice", true),
        ], selected: "3")
        profile.show([
            ("default", "Balanced", true),
            ("short", "Short", true),
            ("detailed", "Detailed", true),
            ("actions", "Action items first", true),
            ("sales", "Customer and commercial", true),
            ("technical", "Technical", true),
        ], selected: "default")
        evening.show([
            ("17", "5 pm", true),
            ("18", "6 pm", true),
            ("19", "7 pm", true),
            ("20", "8 pm", true),
            ("21", "9 pm", true),
        ], selected: "18")
        retention.show(Self.retentionChoices, selected: "1_week")
        pages.show(Self.retentionChoices, selected: "never")
        process.show([
            ("after_meeting", "After each meeting", true),
            ("evening", "Evening (when the laptop is free)", true),
            ("manual", "Manual only", true),
        ], selected: "after_meeting")
        summary.show([
            ("fast", "Fast (4B)", true),
            ("accurate", accurate, canAccurate),
        ], selected: canAccurate ? "accurate" : "fast")
        summaryPick = canAccurate ? "accurate" : "fast"
        onMac.show([("builtin", "Fast or Accurate", true)], selected: "builtin")

        briefTip.font = .systemFont(ofSize: 12)
        briefTip.textColor = YarnColor.slate
        briefTip.maximumNumberOfLines = 2
        briefTip.preferredMaxLayoutWidth = 380
        briefTip.isHidden = true

        let briefRows = stack([brief, briefTip], spacing: 8)
        briefRows.detachesHiddenViews = true

        let form = stack([
            group(
                "Your Thread",
                caption: "Tell Yarn a little about yourself and how you like your answers. It'll use this when you Ask Yarn.",
                body: thread
            ),

            section("Recording"),
            group(
                "Meeting language",
                caption: "The app stays in English. This is the language of the next recording.",
                body: language
            ),
            group(
                "Your name",
                caption: "On a headphone call, your microphone is labelled with this. Press Return to save.",
                body: nameField
            ),

            group(
                "Keyboard shortcut",
                caption: "Control-Option-Command-R from any app. Press it again to stop. It starts In the room or Headphones, whichever you used last. Yarn has to be open. Recording still only starts when you press something.",
                body: hotkey
            ),

            section("Speakers"),
            settingsCard(stack([recognise, identify, suggest], spacing: 16)),
            group(
                "Who counts as a speaker",
                caption: "A voice with less speech than this in total is not listed, numbered or asked about. Its words stay in the transcript.",
                body: floor
            ),

            section("Transcription and models"),
            group("Summary model", body: summary),
            group(
                "On this Mac",
                caption: "Another model already in Ollama, when Ollama is open. Summaries and Ask use it. Tidy-up stays on Fast or Accurate. Yarn does not download a model.",
                body: onMac
            ),
            group(
                "Summary style",
                caption: "What a summary leans towards. It never adds anything that was not said.",
                body: profile
            ),
            group(
                "Glossary",
                caption: "Words Whisper mishears. One per line: what it writes, an equals sign, then the spelling you want.",
                body: glossary
            ),
            settingsCard(stack([cleanup], spacing: 0)),

            section("Storage and retention"),
            group(
                "Audio retention",
                caption: "When do you want Yarn to automatically delete your audio recordings? It saves space and increases privacy. A recording that never became a transcript is kept.",
                body: retention
            ),
            group(
                "Saved web pages",
                caption: "Space saver. How long a meeting keeps its saved page before Yarn removes it. After that the page is made again whenever you open the meeting from the Library. Transcripts, notes, summaries and audio are never touched by this.",
                body: pages
            ),

            section("Brief and summaries"),
            group("Process when", body: process),
            group(
                "Evening starts at",
                caption: "Used when Process when is set to Evening.",
                body: evening
            ),
            settingsCard(stack([pressure], spacing: 0)),
            settingsCard(briefRows),

            section("External AI apps"),
            externalCard(),

            section("Tools"),
            settingsCard(stack([
                link("Import a recording", #selector(importRecording), self),
                link("Import a transcript", #selector(importTranscript), self),
                link("Pause processing", #selector(pauseProcessing), self),
                link("Resume processing", #selector(resumeProcessing), self),
                link("Open transcripts folder", #selector(openTranscripts), self),
                link("Open Ollama", #selector(openOllama), self),
                link("Diagnostics", #selector(diagnostics), self),
                link("Logs", #selector(showLogs), self),
            ], spacing: 2)),
        ], spacing: 12)

        let scroller = NSScrollView()
        scroller.translatesAutoresizingMaskIntoConstraints = false
        scroller.drawsBackground = false
        scroller.borderType = .noBorder
        scroller.hasVerticalScroller = true
        scroller.autohidesScrollers = true
        scroller.contentView = SettingClip()
        let doc = SettingDoc()
        doc.translatesAutoresizingMaskIntoConstraints = false
        scroller.documentView = doc
        form.translatesAutoresizingMaskIntoConstraints = false
        doc.addSubview(form)

        style(status, size: 12, weight: .regular, color: YarnColor.slate)
        status.maximumNumberOfLines = 2
        status.translatesAutoresizingMaskIntoConstraints = false

        content.addSubview(header)
        content.addSubview(scroller)
        content.addSubview(status)

        NSLayoutConstraint.activate([
            mark.widthAnchor.constraint(equalToConstant: 72),
            mark.heightAnchor.constraint(equalToConstant: 36),
            header.topAnchor.constraint(equalTo: content.safeAreaLayoutGuide.topAnchor, constant: 4),
            header.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            header.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),

            scroller.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 12),
            scroller.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            scroller.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -8),

            status.topAnchor.constraint(equalTo: scroller.bottomAnchor, constant: 8),
            status.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            status.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            status.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),

            doc.widthAnchor.constraint(equalTo: scroller.contentView.widthAnchor),
            form.topAnchor.constraint(equalTo: doc.topAnchor, constant: 2),
            form.leadingAnchor.constraint(equalTo: doc.leadingAnchor),
            form.trailingAnchor.constraint(equalTo: doc.trailingAnchor, constant: -12),
            form.bottomAnchor.constraint(equalTo: doc.bottomAnchor, constant: -8),
        ])
    }

    /// The switch, and under it the apps and what switching it on means.
    /// The apps only show once it is on, so the warning is read first.
    private func externalCard() -> NSView {
        let warning = label(
            "With this on, Yarn is no longer a fully local setup. Recording, transcription, summaries and Ask Yarn still run on this Mac, but an app you connect receives the meeting passages it searches for, and they go wherever that app sends them. Yarn cannot see or control that.",
            size: 12,
            weight: .regular,
            color: YarnColor.warn
        )
        warning.maximumNumberOfLines = 8
        warning.preferredMaxLayoutWidth = 380
        let pick = label("Choose an app to connect:", size: 12, weight: .regular, color: YarnColor.slate)
        let body = stack([
            warning,
            pick,
            link("Claude", #selector(connectClaude), self),
            link("Cursor", #selector(connectCursor), self),
            link("ChatGPT", #selector(connectChatGPT), self),
        ], spacing: 6)
        body.isHidden = true
        externalBody = body
        let rows = stack([external, body], spacing: 12)
        rows.detachesHiddenViews = true
        return settingsCard(rows)
    }

    private func section(_ title: String) -> NSView {
        let heading = label(title.uppercased(), size: 11, weight: .semibold, color: YarnColor.slate)
        heading.setAccessibilityRole(.staticText)
        heading.setAccessibilityLabel(title + " settings")
        let box = NSView()
        box.translatesAutoresizingMaskIntoConstraints = false
        heading.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(heading)
        NSLayoutConstraint.activate([
            heading.topAnchor.constraint(equalTo: box.topAnchor, constant: 10),
            heading.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 4),
            heading.bottomAnchor.constraint(equalTo: box.bottomAnchor),
        ])
        return box
    }

    private func group(_ title: String, caption: String? = nil, body: NSView) -> NSView {
        var rows: [NSView] = [label(title, size: 13, weight: .semibold, color: YarnColor.ink)]
        if let caption {
            let line = label(caption, size: 12, weight: .regular, color: YarnColor.slate)
            line.maximumNumberOfLines = 4
            line.preferredMaxLayoutWidth = 380
            rows.append(line)
        }
        rows.append(body)
        return settingsCard(stack(rows, spacing: 8))
    }

    /// White with a grey edge. The shared blue card wash looked pink here.
    private func settingsCard(_ content: NSView) -> NSView {
        let box = SettingPanel()
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
        return box
    }

    func reload() {
        let data = json(paths.root + "/.yarn-settings.json")
        thread.setText(text(data["thread"]))
        let languageID = text(data["meeting_language"])
        language.select(known(languageID, fallback: "en", allowed: ["en", "auto", "ja", "zh", "es", "fr", "de", "ko"]))
        recognise.setOn(data["recognise_known"] == nil ? true : boolean(data["recognise_known"]))
        let hot = text(data["record_hotkey"])
        hotkey.select((hot == "on" || hot == "room" || hot == "call") ? "on" : "off")
        if data["glossary"] != nil {
            glossary.setText(text(data["glossary"]))
        } else {
            loadGlossary()
        }
        // A missing key means the default the pipeline itself uses.
        identify.setOn(data["identify_popup"] == nil ? true : boolean(data["identify_popup"]))
        suggest.setOn(data["suggest_names_popup"] == nil ? true : boolean(data["suggest_names_popup"]))
        cleanup.setOn(data["cleanup_enabled"] == nil ? true : boolean(data["cleanup_enabled"]))
        pressure.setOn(data["pause_on_pressure"] == nil ? true : boolean(data["pause_on_pressure"]))
        let floorValue = (data["min_participant_s"] as? NSNumber)?.doubleValue ?? 3
        floor.select(known(String(Int(floorValue)), fallback: "3", allowed: ["0", "2", "3", "5"]))
        profile.select(known(text(data["summary_profile"]), fallback: "default", allowed: ["default", "short", "detailed", "actions", "sales", "technical"]))
        evening.select(known(text(data["evening_hour"]), fallback: "18", allowed: ["17", "18", "19", "20", "21"]))
        if window.firstResponder !== nameField.currentEditor() {
            nameField.stringValue = Self.ownName()
        }
        let kept = text(data["audio_retention"])
        retention.select(kept == "month" ? "1_month" : known(kept, fallback: "1_week", allowed: Self.retentionIDs))
        // The first version of Space saver was a switch that meant a month.
        var keptPages = text(data["page_retention"])
        if keptPages.isEmpty, boolean(data["space_saver"]) { keptPages = "1_month" }
        savedPages = known(keptPages, fallback: "never", allowed: Self.retentionIDs)
        pages.select(savedPages)
        refreshExternal(saved: data["external_apps"])
        process.select(known(text(data["summarize_when"]), fallback: "after_meeting", allowed: ["after_meeting", "evening", "manual"]))
        let briefOn = boolean(data["brief_on_login"])
        brief.setOn(briefOn)
        let manual = integer(data["brief_manual_count"]) ?? 0
        briefTip.isHidden = briefOn || manual < 3
        let model = text(data["summary_model"])
        let folded = model.lowercased()
        let knownAccurate = folded == "accurate" || folded == "9b" || folded == "qwen3.5:9b"
        let knownFast = folded.isEmpty || folded == "fast" || folded == "4b" || folded == "qwen3.5:4b"
        let remembered = text(data["summary_builtin"]).lowercased()
        if knownAccurate {
            summaryPick = "accurate"
        } else if knownFast {
            summaryPick = folded.isEmpty && canAccurate ? "accurate" : "fast"
        } else if remembered == "accurate" || remembered == "fast" {
            summaryPick = remembered
        } else {
            summaryPick = canAccurate ? "accurate" : "fast"
        }
        summary.select(summaryPick)
        var options: [(String, String, Bool)] = [("builtin", "Fast or Accurate", true)]
        var seen = Set<String>()
        for name in onMacModels where seen.insert(name).inserted {
            options.append((name, name, true))
        }
        let custom = (knownAccurate || knownFast) ? "" : model
        if !custom.isEmpty && seen.insert(custom).inserted {
            options.append((custom, custom, true))
        }
        onMac.show(options, selected: custom.isEmpty ? "builtin" : custom)
    }

    /// Other Ollama models, from a running Ollama. A closed Ollama leaves
    /// the row on Fast or Accurate.
    private func loadOnMac() {
        let ma = paths.ma
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let got = Self.capture(ma, ["ollama-models", "--json"])
            let names = got["models"] as? [String] ?? []
            DispatchQueue.main.async {
                guard let self, !self.writing else { return }
                self.onMacModels = names
                self.reload()
            }
        }
    }

    private func known(_ raw: String, fallback: String, allowed: [String]) -> String {
        allowed.contains(raw) ? raw : fallback
    }

    private func write(_ key: String, _ value: String) {
        writeGeneration += 1
        let generation = writeGeneration
        writing = true
        let ma = paths.ma
        writer.async { [weak self] in
            let result = Self.run(ma, ["setting", key, value])
            DispatchQueue.main.async {
                guard let self, generation == self.writeGeneration else { return }
                self.writing = false
                self.reload()
                if result.ok {
                    self.flash("Saved.")
                } else {
                    self.flash(result.err.isEmpty ? "That setting didn\u{2019}t save." : result.err, warn: true)
                }
            }
        }
    }

    private func flash(_ text: String, warn: Bool = false) {
        status.stringValue = text
        status.textColor = warn ? YarnColor.warn : YarnColor.slate
        let generation = writeGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { [weak self] in
            guard let self, generation == self.writeGeneration, self.status.stringValue == text else { return }
            self.status.stringValue = ""
        }
    }

    /// The list already in config.toml, until Settings has saved its own.
    private func loadGlossary() {
        let ma = paths.ma
        writer.async { [weak self] in
            let got = Self.capture(ma, ["glossary", "--json"])
            DispatchQueue.main.async {
                self?.glossary.showLoaded(text(got["text"]))
            }
        }
    }

    /// The name is in config.toml with the other names, not in the settings
    /// file, because it is part of how every transcript is labelled.
    private static func ownName() -> String {
        let env = ProcessInfo.processInfo.environment["MEETING_ARCHIVE_CONFIG"] ?? ""
        let path = env.isEmpty
            ? NSHomeDirectory() + "/.config/meeting-archive/config.toml"
            : env
        guard let body = try? String(contentsOfFile: path, encoding: .utf8) else { return "" }
        for line in body.split(separator: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("own_label"), let eq = trimmed.firstIndex(of: "=") else { continue }
            var value = trimmed[trimmed.index(after: eq)...].trimmingCharacters(in: .whitespaces)
            if value.hasPrefix("\""), value.hasSuffix("\""), value.count >= 2 {
                value = String(value.dropFirst().dropLast())
            }
            return value.replacingOccurrences(of: "\\\"", with: "\"")
        }
        return ""
    }

    @objc private func saveName() {
        let name = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name != Self.ownName() else { return }
        window.makeFirstResponder(nil)
        write("own_name", name)
    }

    /// Shortening how long pages are kept is the one setting that removes
    /// files, so it says what will go and how much, and waits for a yes.
    /// Choosing Never removes nothing.
    private func pagesChanged(_ id: String) {
        guard id != savedPages else { return }
        if id == "never" {
            pages.select(id)
            write("page_retention", id)
            return
        }
        let ma = paths.ma
        let wording = Self.retentionChoices.first { $0.0 == id }?.1.lowercased() ?? id
        writer.async { [weak self] in
            let preview = Self.capture(ma, ["space-saver", "--json", "--keep", id])
            DispatchQueue.main.async {
                guard let self else { return }
                let count = integer(preview["count"]) ?? 0
                let size = text(preview["label"])
                let alert = NSAlert()
                alert.messageText = "Keep saved pages for \(wording)?"
                alert.informativeText = count > 0
                    ? "This removes \(count) saved web page\(count == 1 ? "" : "s") of older meetings now and frees \(size). Each one is made again when you open that meeting from the Library. Your transcripts, notes, summaries and audio are not touched."
                    : "Nothing needs removing right now. From here on a meeting keeps its saved page for \(wording). Your transcripts, notes, summaries and audio are not touched."
                alert.addButton(withTitle: "Keep for \(wording)")
                alert.addButton(withTitle: "Cancel")
                alert.beginSheetModal(for: self.window) { answer in
                    guard answer == .alertFirstButtonReturn else {
                        self.pages.select(self.savedPages)
                        return
                    }
                    self.pages.select(id)
                    self.savedPages = id
                    self.write("page_retention", id)
                    self.writer.async {
                        let done = Self.capture(ma, ["space-saver", "--purge", "--json", "--keep", id])
                        DispatchQueue.main.async {
                            if (integer(done["count"]) ?? 0) > 0 {
                                self.flash("Freed \(text(done["label"])).")
                            }
                        }
                    }
                }
            }
        }
    }

    /// The saved switch if there is one. Otherwise ask whether an app is
    /// already connected: someone who connected Claude before this switch
    /// existed should see it on, because on their Mac it is.
    private func refreshExternal(saved: Any?) {
        if saved != nil {
            showExternal(boolean(saved))
            return
        }
        let ma = paths.ma
        writer.async { [weak self] in
            let status = Self.capture(ma, ["connect", "status"])
            DispatchQueue.main.async { self?.showExternal(boolean(status["enabled"])) }
        }
    }

    private func showExternal(_ on: Bool) {
        externalOn = on
        external.setOn(on)
        externalBody?.isHidden = !on
    }

    private func externalChanged(_ on: Bool) {
        if on {
            showExternal(true)
            write("external_apps", "on")
            return
        }
        let alert = NSAlert()
        alert.messageText = "Disconnect external AI apps?"
        alert.informativeText = "Yarn removes itself from Claude and Cursor, so they can no longer search your meetings. Restart those apps afterwards. If you added Yarn to ChatGPT by hand, remove it there under Settings, Connectors. Nothing on this Mac is deleted."
        alert.addButton(withTitle: "Disconnect")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] answer in
            guard let self else { return }
            guard answer == .alertFirstButtonReturn else {
                self.showExternal(true)
                return
            }
            self.showExternal(false)
            self.simple(["connect", "disconnect"], done: "Disconnected. Yarn is fully on this Mac again.")
        }
    }

    @objc private func importRecording() { importFile("audio") }
    @objc private func importTranscript() { importFile("transcript") }

    private func importFile(_ kind: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        // The type list is a hint. allowsOtherFileTypes keeps a recording
        // selectable when macOS does not recognise its extension.
        panel.allowsOtherFileTypes = true
        if kind == "audio" {
            panel.allowedContentTypes = [.wav, .mp3, .mpeg4Audio, .mpeg4Movie, .quickTimeMovie, .audio]
            panel.message = "Choose a recording to add to Yarn. A long meeting is copied in first, then transcribed on this Mac."
        } else {
            panel.allowedContentTypes = [.plainText, .text, .utf8PlainText]
            panel.message = "Choose a transcript to add to Yarn."
        }
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.beginSheetModal(for: window) { [weak self] answer in
            guard let self, answer == .OK, let url = panel.url else { return }
            let ma = self.paths.ma
            let name = url.lastPathComponent
            self.flash(
                kind == "audio"
                    ? "Copying \(name) in. A long recording takes a few minutes."
                    : "Importing \(name)\u{2026}"
            )
            // The open panel's permission does not pass to `ma`. Downloads is
            // one of the folders a child process is silently refused, so the
            // file is copied into the archive while this window can still read it.
            self.writer.async {
                let accessing = url.startAccessingSecurityScopedResource()
                defer { if accessing { url.stopAccessingSecurityScopedResource() } }
                let staged: (path: String, temporary: Bool)
                do {
                    staged = try self.stageImport(url)
                } catch {
                    DispatchQueue.main.async {
                        self.flash("That file could not be read. \(error.localizedDescription)", warn: true)
                    }
                    return
                }
                let result = Self.run(ma, [kind == "audio" ? "import-audio" : "import-transcript", staged.path])
                if staged.temporary {
                    try? FileManager.default.removeItem(atPath: staged.path)
                }
                DispatchQueue.main.async {
                    self.flash(
                        result.ok ? "Imported. It will appear once it has been processed." : (result.err.isEmpty ? "That file could not be imported." : result.err),
                        warn: !result.ok
                    )
                }
            }
        }
    }

    /// Copy a picked file into the archive so `ma` can read it.
    /// A file that is already inside the archive is used where it is.
    private func stageImport(_ url: URL) throws -> (path: String, temporary: Bool) {
        let root = URL(fileURLWithPath: paths.root, isDirectory: true)
        let source = url.standardizedFileURL
        if source.path.hasPrefix(root.standardizedFileURL.path + "/") {
            return (source.path, false)
        }
        let folder = root.appendingPathComponent("imports", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let leaf = source.lastPathComponent.isEmpty ? "import" : source.lastPathComponent
        let dest = folder.appendingPathComponent(leaf)
        if FileManager.default.fileExists(atPath: dest.path) {
            try FileManager.default.removeItem(at: dest)
        }
        try FileManager.default.copyItem(at: source, to: dest)
        return (dest.path, true)
    }

    @objc private func pauseProcessing() { simple(["pause"], done: "Processing is paused. Recording is not affected.") }
    @objc private func resumeProcessing() { simple(["resume"], done: "Processing resumed.") }

    private func simple(_ args: [String], done: String) {
        let ma = paths.ma
        writer.async { [weak self] in
            let result = Self.run(ma, args)
            DispatchQueue.main.async {
                self?.flash(result.ok ? done : (result.err.isEmpty ? "That didn\u{2019}t work." : result.err), warn: !result.ok)
            }
        }
    }

    @objc private func openTranscripts() {
        NSWorkspace.shared.open(URL(fileURLWithPath: paths.root + "/transcripts", isDirectory: true))
    }

    @objc private func openOllama() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        task.arguments = ["-a", "Ollama"]
        do { try task.run() } catch { flash("Ollama isn\u{2019}t installed on this Mac.", warn: true) }
    }

    /// Run `ma` and read the JSON it prints. Empty on any failure.
    private static func capture(_ bin: String, _ args: [String]) -> [String: Any] {
        guard FileManager.default.isExecutableFile(atPath: bin) else { return [:] }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: bin)
        task.arguments = args
        let out = Pipe()
        task.standardOutput = out
        task.standardError = FileHandle.nullDevice
        guard (try? task.run()) != nil else { return [:] }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj
    }

    @objc private func connectClaude() { connect("claude") }
    @objc private func connectCursor() { connect("cursor") }
    @objc private func connectChatGPT() { connect("chatgpt") }

    private func connect(_ target: String) {
        let ma = paths.ma
        guard FileManager.default.isExecutableFile(atPath: ma) else {
            flash("That isn\u{2019}t available yet.", warn: true)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            _ = Self.run(ma, ["connect", target])
        }
    }

    @objc private func diagnostics() {
        let ma = paths.ma
        guard FileManager.default.isExecutableFile(atPath: ma) else {
            flash("Diagnostics isn\u{2019}t available yet.", warn: true)
            return
        }
        let quoted = "'" + ma.replacingOccurrences(of: "'", with: "'\\''") + "' doctor"
        let source = "tell application \"Terminal\" to do script \"\(quoted)\""
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        task.arguments = ["-e", source]
        do {
            try task.run()
        } catch {
            flash("Diagnostics couldn\u{2019}t open Terminal.", warn: true)
        }
    }

    @objc private func showLogs() {
        NSWorkspace.shared.open(URL(fileURLWithPath: paths.root + "/logs", isDirectory: true))
    }

    private static func run(_ bin: String, _ args: [String]) -> (ok: Bool, err: String) {
        guard FileManager.default.isExecutableFile(atPath: bin) else {
            return (false, "That isn\u{2019}t available yet.")
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: bin)
        task.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["MA_BIN"] = bin
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        task.environment = env
        let errPipe = Pipe()
        task.standardOutput = FileHandle.nullDevice
        task.standardError = errPipe
        do {
            try task.run()
        } catch {
            return (false, "That isn\u{2019}t available yet.")
        }
        task.waitUntilExit()
        let err = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return (task.terminationStatus == 0, err)
    }

    private static func machineCanRunAccurate() -> Bool {
        // The menu uses the same cutoff: above 10 GB, Accurate is offered.
        ProcessInfo.processInfo.physicalMemory > 10_737_418_240
    }
}

final class SettingChoices: NSStackView {
    var onPick: ((String) -> Void)?
    private var buttons: [SettingChoice] = []

    init() {
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .leading
        spacing = 2
        translatesAutoresizingMaskIntoConstraints = false
    }

    required init?(coder: NSCoder) { nil }

    func show(_ options: [(String, String, Bool)], selected: String) {
        buttons.forEach { button in
            removeArrangedSubview(button)
            button.removeFromSuperview()
        }
        buttons = options.map { id, title, enabled in
            let button = SettingChoice()
            button.optionID = id
            button.title = title
            button.available = enabled
            button.chosen = id == selected
            button.target = self
            button.action = #selector(picked(_:))
            button.heightAnchor.constraint(equalToConstant: 30).isActive = true
            addArrangedSubview(button)
            button.widthAnchor.constraint(equalTo: widthAnchor).isActive = true
            return button
        }
    }

    func select(_ id: String) {
        for button in buttons {
            button.chosen = button.optionID == id
        }
    }

    @objc private func picked(_ sender: SettingChoice) {
        guard sender.available else { return }
        onPick?(sender.optionID)
    }
}

/// Not a system button. macOS paints those wells in a warm grey, and on
/// this dark card that grey reads as pink.
final class SettingChoice: NSControl {
    var optionID = ""
    var title = "" { didSet { needsDisplay = true } }
    var chosen = false { didSet { needsDisplay = true } }
    var available = true { didSet { needsDisplay = true } }

    override var wantsUpdateLayer: Bool { false }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        refusesFirstResponder = true
    }

    required init?(coder: NSCoder) { nil }

    override func resetCursorRects() {
        if available { addCursorRect(bounds, cursor: .pointingHand) }
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
        if available { NSCursor.pointingHand.set() }
    }

    override func mouseUp(with event: NSEvent) {
        guard available else { return }
        let point = convert(event.locationInWindow, from: nil)
        guard bounds.contains(point), let action else { return }
        NSApp.sendAction(action, to: target, from: self)
    }

    override func draw(_ dirtyRect: NSRect) {
        let color: NSColor = available ? (chosen ? YarnColor.navy : YarnColor.ink) : YarnColor.slate
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 13, weight: chosen ? .semibold : .regular),
            .foregroundColor: color,
        ]
        let shown = title as NSString
        let size = shown.size(withAttributes: attrs)
        let width = max(0, bounds.width - (chosen ? 28 : 16))
        shown.draw(
            with: NSRect(x: 10, y: (bounds.height - size.height) / 2, width: width, height: size.height),
            options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine],
            attributes: attrs
        )
        if chosen {
            YarnColor.blue.setFill()
            NSBezierPath(ovalIn: NSRect(x: bounds.width - 16, y: (bounds.height - 6) / 2, width: 6, height: 6)).fill()
        }
    }
}

final class SettingSwitch: NSView {
    let toggle = NSSwitch()
    var onChange: ((Bool) -> Void)?
    private var applying = false

    init(title: String, caption: String) {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        let heading = label(title, size: 13, weight: .semibold, color: YarnColor.ink)
        let detail = label(caption, size: 12, weight: .regular, color: YarnColor.slate)
        detail.maximumNumberOfLines = 3
        detail.preferredMaxLayoutWidth = 360
        toggle.target = self
        toggle.action = #selector(changed)
        toggle.setContentHuggingPriority(.required, for: .horizontal)
        heading.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let top = NSStackView(views: [heading, toggle])
        top.orientation = .horizontal
        top.alignment = .centerY
        top.distribution = .fill
        top.translatesAutoresizingMaskIntoConstraints = false
        let column = NSStackView(views: [top, detail])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 4
        column.translatesAutoresizingMaskIntoConstraints = false
        addSubview(column)
        NSLayoutConstraint.activate([
            column.topAnchor.constraint(equalTo: topAnchor),
            column.leadingAnchor.constraint(equalTo: leadingAnchor),
            column.trailingAnchor.constraint(equalTo: trailingAnchor),
            column.bottomAnchor.constraint(equalTo: bottomAnchor),
            top.widthAnchor.constraint(equalTo: column.widthAnchor),
            detail.widthAnchor.constraint(equalTo: column.widthAnchor),
        ])
    }

    required init?(coder: NSCoder) { nil }

    func setOn(_ on: Bool) {
        applying = true
        toggle.state = on ? .on : .off
        applying = false
    }

    @objc private func changed() {
        if applying { return }
        onChange?(toggle.state == .on)
    }
}

private final class SettingPanel: NSView {
    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        let dark = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        layer?.cornerRadius = 12
        layer?.borderWidth = 1
        layer?.backgroundColor = (dark ? yarn(0x161C24) : yarn(0xFFFFFF)).cgColor
        layer?.borderColor = (dark ? yarn(0x2C3642) : yarn(0xE2E6EC)).cgColor
    }
}

private final class SettingDoc: NSView {
    override var isFlipped: Bool { true }
}

private final class SettingClip: NSClipView {
    override var isFlipped: Bool { true }
}

/// A short note about the person using Yarn. The grey sentence is an example,
/// shown only while the box is empty, and it is never saved.
///
/// The box has to wrap to its own width, and the letters have to be a plain
/// colour. On macOS 13 and 14 a layer behind the text view draws the cursor
/// and hides the letters, and a text view left at its default width draws
/// the example as one line that runs out of the box.
final class ThreadEditor: NSView, NSTextViewDelegate {
    static let example = "I'm Jacob. I manage Solution Consulting. Keep answers short, lead with decisions, and tell me who's doing what next."
    var onCommit: ((String) -> Void)?
    var limit = 2000
    var placeholderText = ThreadEditor.example {
        didSet { textView.placeholder = placeholderText }
    }
    private let textView = ThreadText()
    private let scroll = NSScrollView()
    private var saved = ""
    private var touched = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false

        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.contentView = ThreadClip()
        scroll.automaticallyAdjustsContentInsets = false

        let wrapping = NSMutableParagraphStyle()
        wrapping.lineBreakMode = .byWordWrapping
        textView.placeholder = placeholderText
        textView.delegate = self
        textView.isRichText = false
        textView.importsGraphics = false
        textView.font = .systemFont(ofSize: 13)
        textView.drawsBackground = true
        textView.isEditable = true
        textView.isSelectable = true
        textView.allowsUndo = true
        textView.textContainerInset = NSSize(width: 6, height: 8)
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isHorizontallyResizable = false
        textView.isVerticallyResizable = true
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.heightTracksTextView = false
        textView.textContainer?.lineFragmentPadding = 0
        textView.defaultParagraphStyle = wrapping
        scroll.documentView = textView

        addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: topAnchor, constant: 1),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 1),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -1),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -1),
            heightAnchor.constraint(equalToConstant: 108),
        ])
        applyColours()
    }

    required init?(coder: NSCoder) { nil }

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let dark = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        let fill = dark ? yarn(0x10161C) : yarn(0xF7F8FA)
        let stroke = dark ? yarn(0x2C3642) : yarn(0xE2E6EC)
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 8, yRadius: 8)
        fill.setFill()
        path.fill()
        stroke.setStroke()
        path.lineWidth = 1
        path.stroke()
    }

    override func layout() {
        super.layout()
        fitText()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyColours()
        fitText()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyColours()
        needsDisplay = true
    }

    /// Plain colours, resolved for the current appearance. A dynamic colour
    /// inside this card can come out the same shade as the background, so
    /// the cursor moves and the words do not show.
    private func applyColours() {
        let dark = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        let ink = dark ? yarn(0xE7EEF6) : yarn(0x19293D)
        let fill = dark ? yarn(0x10161C) : yarn(0xF7F8FA)
        let ghost = (dark ? yarn(0x9AAFC3) : yarn(0x617188)).withAlphaComponent(0.8)
        let wrapping = (textView.defaultParagraphStyle?.mutableCopy() as? NSMutableParagraphStyle)
            ?? NSMutableParagraphStyle()
        wrapping.lineBreakMode = .byWordWrapping
        textView.backgroundColor = fill
        textView.textColor = ink
        textView.insertionPointColor = yarn(0x167DFF)
        textView.typingAttributes = [
            .font: NSFont.systemFont(ofSize: 13),
            .foregroundColor: ink,
            .paragraphStyle: wrapping,
        ]
        textView.ghost = ghost
        if textView.string.isEmpty == false {
            textView.textStorage?.addAttributes(textView.typingAttributes, range: NSRange(location: 0, length: textView.string.utf16.count))
        }
        textView.needsDisplay = true
    }

    /// The text view's default width is far wider than the card, so the
    /// example and the typing both run off the right edge.
    private func fitText() {
        let width = scroll.contentSize.width
        guard width > 20 else { return }
        textView.minSize = NSSize(width: width, height: 0)
        textView.maxSize = NSSize(width: width, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.containerSize = NSSize(width: width, height: CGFloat.greatestFiniteMagnitude)
        if abs(textView.frame.width - width) > 0.5 {
            textView.frame.size.width = width
        }
        textView.needsDisplay = true
    }

    func setText(_ value: String) {
        if window?.firstResponder == textView { return }
        if textView.string != value {
            textView.string = value
            applyColours()
        }
        saved = value.trimmingCharacters(in: .whitespacesAndNewlines)
        textView.needsDisplay = true
    }

    /// The spelling list can arrive after the window is open. Apply it only
    /// while the box is still what was last shown. Typing, or a save, wins.
    func showLoaded(_ value: String) {
        if touched { return }
        let current = textView.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if current != saved { return }
        setText(value)
    }

    func commit() {
        let value = String(textView.string.prefix(limit))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if value == saved { return }
        touched = true
        saved = value
        onCommit?(value)
    }

    func textDidChange(_ notification: Notification) {
        if textView.string.count > limit {
            textView.string = String(textView.string.prefix(limit))
        }
        textView.needsDisplay = true
    }

    func textDidEndEditing(_ notification: Notification) {
        commit()
    }
}

/// A layer here is what hides the letters on macOS 13 and 14.
private final class ThreadClip: NSClipView {
    override var wantsLayer: Bool {
        get { false }
        set { }
    }
}

private final class ThreadText: NSTextView {
    var ghost = yarn(0x617188).withAlphaComponent(0.8)
    var placeholder = ThreadEditor.example

    override var wantsLayer: Bool {
        get { false }
        set { }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty else { return }
        let shown = NSAttributedString(
            string: placeholder,
            attributes: [
                .font: font ?? NSFont.systemFont(ofSize: 13),
                .foregroundColor: ghost,
                .paragraphStyle: defaultParagraphStyle ?? {
                    let style = NSMutableParagraphStyle()
                    style.lineBreakMode = .byWordWrapping
                    return style
                }(),
            ]
        )
        let origin = textContainerOrigin
        let wrap = textContainer?.containerSize.width ?? bounds.width
        let width = min(bounds.width, wrap > 20 ? wrap : bounds.width)
        shown.draw(with: NSRect(
            x: origin.x,
            y: origin.y,
            width: max(20, width - origin.x),
            height: max(bounds.height, 80)
        ), options: [.usesLineFragmentOrigin])
    }
}
