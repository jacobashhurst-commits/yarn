// Ask Yarn. The window is Yarn's. Ollama only writes the reply, from the
// meetings already on this Mac. History lives in asks/chat.json.

import AppKit

final class AskChat: NSObject, NSWindowDelegate {
    private let paths: Paths
    private let window: NSWindow
    private let transcript = NSStackView()
    private let scroller = NSScrollView()
    private let composer = AskComposer()
    private let askButton = AskButton()
    private var saved: [(role: String, text: String)] = []
    private var recallSlot = -1
    private var recallText: String?
    private var pending: [(role: String, text: String)] = []
    private var busy = false
    private var laidOutWidth: CGFloat = 0
    private var threads: [Thread] = []
    private var activeID = ""
    private let history = NSStackView()
    private let historyScroll = NSScrollView()

    private static var current: AskChat?

    static func show(paths: Paths) {
        if current == nil {
            current = AskChat(paths: paths)
        }
        current?.reloadIfIdle()
        current?.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        current?.window.makeFirstResponder(current?.composer.textView)
    }

    init(paths: Paths) {
        self.paths = paths
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 980, height: 680),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "Ask Yarn"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = YarnColor.canvas
        window.minSize = NSSize(width: 780, height: 480)
        window.isReleasedWhenClosed = false
        window.delegate = self
        build()
        window.center()
    }

    private func build() {
        let content = NSView()
        window.contentView = content

        let mark = NSImageView()
        if let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png") {
            mark.image = NSImage(contentsOf: url)
        }
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Ask Yarn")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        title.textColor = YarnColor.navy

        let subtitle = NSTextField(wrappingLabelWithString: "Ollama answers from your meetings, on this Mac.")
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = YarnColor.slate
        subtitle.preferredMaxLayoutWidth = 300

        let titles = NSStackView(views: [title, subtitle])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 2

        let header = NSStackView(views: [mark, titles])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 10
        header.translatesAutoresizingMaskIntoConstraints = false

        transcript.orientation = .vertical
        transcript.alignment = .leading
        transcript.spacing = 10
        transcript.translatesAutoresizingMaskIntoConstraints = false

        let doc = NSView()
        doc.translatesAutoresizingMaskIntoConstraints = false
        doc.addSubview(transcript)

        scroller.translatesAutoresizingMaskIntoConstraints = false
        scroller.hasVerticalScroller = true
        scroller.drawsBackground = false
        scroller.borderType = .noBorder
        scroller.documentView = doc

        composer.onSubmit = { [weak self] in self?.send() }
        composer.onRecall = { [weak self] delta in self?.recall(delta) }
        composer.onEdit = { [weak self] text in self?.composerEdited(text) }
        composer.setContentHuggingPriority(.defaultLow, for: .horizontal)

        askButton.title = "Ask"
        askButton.isBordered = false
        askButton.isEnabled = false
        askButton.target = self
        askButton.action = #selector(send)
        askButton.translatesAutoresizingMaskIntoConstraints = false
        askButton.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [composer, askButton])
        row.orientation = .horizontal
        row.alignment = .bottom
        row.distribution = .fill
        row.spacing = 8
        row.translatesAutoresizingMaskIntoConstraints = false

        let hint = NSTextField(labelWithString: "Return to ask. Shift-return for a new line.")
        hint.font = .systemFont(ofSize: 11)
        hint.textColor = YarnColor.slate
        hint.translatesAutoresizingMaskIntoConstraints = false

        let compose = NSStackView(views: [row, hint])
        compose.orientation = .vertical
        compose.alignment = .leading
        compose.spacing = 6
        compose.translatesAutoresizingMaskIntoConstraints = false

        history.orientation = .vertical
        history.alignment = .leading
        history.spacing = 2
        history.translatesAutoresizingMaskIntoConstraints = false
        let historyDoc = FlippedView()
        historyDoc.translatesAutoresizingMaskIntoConstraints = false
        historyDoc.addSubview(history)
        historyScroll.translatesAutoresizingMaskIntoConstraints = false
        historyScroll.contentView = FlippedClipView()
        historyScroll.hasVerticalScroller = true
        historyScroll.drawsBackground = false
        historyScroll.borderType = .noBorder
        historyScroll.documentView = historyDoc

        let newChat = NSButton(title: "New chat", target: self, action: #selector(startChat))
        newChat.isBordered = false
        newChat.font = .systemFont(ofSize: 13, weight: .semibold)
        newChat.contentTintColor = YarnColor.navy
        newChat.alignment = .left
        newChat.translatesAutoresizingMaskIntoConstraints = false

        let rail = NSView()
        rail.translatesAutoresizingMaskIntoConstraints = false
        rail.wantsLayer = true
        rail.layer?.backgroundColor = YarnColor.cloud.cgColor
        rail.addSubview(newChat)
        rail.addSubview(historyScroll)

        content.addSubview(rail)
        content.addSubview(header)
        content.addSubview(scroller)
        content.addSubview(compose)

        NSLayoutConstraint.activate([
            mark.widthAnchor.constraint(equalToConstant: 72),
            mark.heightAnchor.constraint(equalToConstant: 36),

            rail.topAnchor.constraint(equalTo: content.topAnchor),
            rail.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            rail.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            rail.widthAnchor.constraint(equalToConstant: 210),

            newChat.topAnchor.constraint(equalTo: rail.topAnchor, constant: 16),
            newChat.leadingAnchor.constraint(equalTo: rail.leadingAnchor, constant: 12),
            newChat.trailingAnchor.constraint(equalTo: rail.trailingAnchor, constant: -8),

            historyScroll.topAnchor.constraint(equalTo: newChat.bottomAnchor, constant: 8),
            historyScroll.leadingAnchor.constraint(equalTo: rail.leadingAnchor),
            historyScroll.trailingAnchor.constraint(equalTo: rail.trailingAnchor),
            historyScroll.bottomAnchor.constraint(equalTo: rail.bottomAnchor, constant: -8),

            historyDoc.widthAnchor.constraint(equalTo: historyScroll.contentView.widthAnchor),
            history.topAnchor.constraint(equalTo: historyDoc.topAnchor, constant: 4),
            history.leadingAnchor.constraint(equalTo: historyDoc.leadingAnchor),
            history.trailingAnchor.constraint(equalTo: historyDoc.trailingAnchor),
            history.bottomAnchor.constraint(equalTo: historyDoc.bottomAnchor, constant: -4),

            header.topAnchor.constraint(equalTo: content.topAnchor, constant: 8),
            header.leadingAnchor.constraint(equalTo: rail.trailingAnchor, constant: 16),
            header.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),

            scroller.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 14),
            scroller.leadingAnchor.constraint(equalTo: rail.trailingAnchor, constant: 16),
            scroller.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),

            compose.topAnchor.constraint(equalTo: scroller.bottomAnchor, constant: 12),
            compose.leadingAnchor.constraint(equalTo: rail.trailingAnchor, constant: 16),
            compose.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            compose.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),

            row.widthAnchor.constraint(equalTo: compose.widthAnchor),
            askButton.widthAnchor.constraint(equalToConstant: 72),
            askButton.heightAnchor.constraint(equalToConstant: 36),

            doc.widthAnchor.constraint(equalTo: scroller.contentView.widthAnchor),
            transcript.topAnchor.constraint(equalTo: doc.topAnchor, constant: 4),
            transcript.leadingAnchor.constraint(equalTo: doc.leadingAnchor),
            transcript.trailingAnchor.constraint(equalTo: doc.trailingAnchor),
            transcript.bottomAnchor.constraint(equalTo: doc.bottomAnchor, constant: -4),
        ])
    }

    /// Up walks back through this chat's questions when the box is empty.
    /// A question you are still editing keeps the arrow keys for the caret.
    private func recall(_ delta: Int) -> String? {
        let yours = saved.compactMap { $0.role == "you" ? $0.text : nil }
        let current = composer.text
        let showing = recallText != nil && current == recallText
        if !current.isEmpty && !showing { return nil }
        guard !yours.isEmpty else { return nil }
        if delta < 0 {
            if recallSlot < 0 { recallSlot = yours.count }
            if recallSlot == 0 {
                return recallText ?? yours[0]
            }
            recallSlot -= 1
            recallText = yours[recallSlot]
            return recallText
        }
        if recallSlot < 0 { return nil }
        recallSlot += 1
        if recallSlot >= yours.count {
            resetRecall()
            return ""
        }
        recallText = yours[recallSlot]
        return recallText
    }

    private func resetRecall() {
        recallSlot = -1
        recallText = nil
    }

    private func composerEdited(_ text: String) {
        if text != recallText { resetRecall() }
        let ready = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        askButton.isEnabled = !busy && ready
    }

    @objc private func send() {
        let question = composer.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, !busy else { return }
        busy = true
        resetRecall()
        composer.setText("")
        askButton.isEnabled = false
        ensureThread()
        saved.append((role: "you", text: question))
        touchActive()
        pending = [(role: "yarn", text: "Looking through your meetings…")]
        render()
        let ma = paths.ma
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let reply = Self.run(ma: ma, question: question)
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                self.pending = []
                self.saved.append((role: "yarn", text: reply.text))
                if !reply.ok && self.composer.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    self.composer.setText(question)
                } else {
                    let ready = !self.composer.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    self.askButton.isEnabled = ready
                }
                self.persist()
                self.render()
            }
        }
    }

    private static func run(ma: String, question: String) -> (ok: Bool, text: String) {
        guard FileManager.default.isExecutableFile(atPath: ma) else {
            return (false, "Ask Yarn isn't available yet.")
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: ma)
        task.arguments = ["ask", "--text", question]
        var env = ProcessInfo.processInfo.environment
        env["MA_BIN"] = ma
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        task.environment = env
        let out = Pipe()
        let err = Pipe()
        task.standardOutput = out
        task.standardError = err
        do {
            try task.run()
        } catch {
            return (false, "Ask Yarn isn't available yet.")
        }
        task.waitUntilExit()
        let answer = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let problem = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if task.terminationStatus != 0 {
            return (false, problem.isEmpty ? "Yarn couldn't answer that." : problem)
        }
        return (true, answer.isEmpty ? "Yarn couldn't answer that." : answer)
    }

    private func reloadIfIdle() {
        guard !busy else { return }
        Self.readStore(paths.root + "/asks/chat.json", into: &threads, active: &activeID)
        saved = threads.first { $0.id == activeID }?.turns ?? []
        pending = []
        render()
    }

    private func ensureThread() {
        if threads.contains(where: { $0.id == activeID }) { return }
        let id = UUID().uuidString
        threads.insert(Thread(id: id, updated: Date().timeIntervalSince1970, turns: []), at: 0)
        activeID = id
    }

    private func stashActive() {
        guard let index = threads.firstIndex(where: { $0.id == activeID }) else { return }
        threads[index].turns = saved
    }

    /// Only a new message moves a chat into Today. Opening the window or
    /// resizing it must leave yesterday's chats under Older.
    private func touchActive() {
        guard let index = threads.firstIndex(where: { $0.id == activeID }) else { return }
        threads[index].updated = Date().timeIntervalSince1970
    }

    @objc private func startChat() {
        guard !busy else { return }
        stashActive()
        if saved.isEmpty { return }
        let id = UUID().uuidString
        threads.insert(Thread(id: id, updated: Date().timeIntervalSince1970, turns: []), at: 0)
        activeID = id
        saved = []
        pending = []
        persist()
        render()
    }

    @objc private func pickChat(_ sender: NSButton) {
        guard !busy, let id = sender.identifier?.rawValue, id != activeID else { return }
        stashActive()
        activeID = id
        saved = threads.first { $0.id == id }?.turns ?? []
        pending = []
        render(scroll: true)
    }

    private func persist() {
        stashActive()
        let chats: [[String: Any]] = threads.map { thread in
            [
                "id": thread.id,
                "updated": Int(thread.updated),
                "turns": thread.turns.map { ["role": $0.role, "text": $0.text] },
            ]
        }
        let store: [String: Any] = ["active": activeID, "chats": chats]
        guard let data = try? JSONSerialization.data(withJSONObject: store, options: [.prettyPrinted]) else {
            return
        }
        let url = URL(fileURLWithPath: paths.root + "/asks/chat.json")
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try? data.write(to: url)
    }

    private static func readStore(
        _ path: String,
        into threads: inout [Thread],
        active: inout String
    ) {
        guard let data = FileManager.default.contents(atPath: path),
              let object = try? JSONSerialization.jsonObject(with: data)
        else {
            threads = []
            active = ""
            return
        }
        if let list = object as? [[String: Any]] {
            let turns = Self.turns(list)
            let updated = (try? FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as? Date)?
                .timeIntervalSince1970 ?? 0
            threads = [Thread(id: "legacy", updated: updated, turns: turns)]
            active = "legacy"
            return
        }
        guard let store = object as? [String: Any] else {
            threads = []
            active = ""
            return
        }
        let chats = (store["chats"] as? [[String: Any]]) ?? []
        threads = chats.compactMap { item in
            guard let id = item["id"] as? String, !id.isEmpty else { return nil }
            let updated = TimeInterval(item["updated"] as? Int ?? 0)
            let turns = Self.turns(item["turns"] as? [[String: Any]] ?? [])
            return Thread(id: id, updated: updated, turns: turns)
        }
        active = store["active"] as? String ?? threads.first?.id ?? ""
        if !threads.contains(where: { $0.id == active }) {
            active = threads.first?.id ?? ""
        }
    }

    private static func turns(_ list: [[String: Any]]) -> [(role: String, text: String)] {
        list.compactMap { item in
            guard let role = item["role"] as? String, let text = item["text"] as? String else {
                return nil
            }
            return (role: role, text: text)
        }
    }

    private func render(scroll: Bool = true) {
        transcript.arrangedSubviews.forEach { view in
            transcript.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        let turns = saved + pending
        if turns.isEmpty {
            let hint = NSTextField(wrappingLabelWithString: "Ask about a decision, a person, or something that was said.")
            hint.font = .systemFont(ofSize: 13)
            hint.textColor = YarnColor.slate
            hint.preferredMaxLayoutWidth = 360
            transcript.addArrangedSubview(hint)
        }
        for turn in turns {
            let row = bubble(turn.text, mine: turn.role == "you")
            transcript.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: transcript.widthAnchor).isActive = true
        }
        scroller.layoutSubtreeIfNeeded()
        laidOutWidth = scroller.contentView.bounds.width
        renderHistory()
        if scroll { scrollToEnd() }
    }

    private func renderHistory() {
        stashActive()
        history.arrangedSubviews.forEach { view in
            history.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        let start = Calendar.current.startOfDay(for: Date()).timeIntervalSince1970
        let ordered = threads.filter { !$0.turns.isEmpty }.sorted { $0.updated > $1.updated }
        let today = ordered.filter { $0.updated >= start }
        let older = ordered.filter { $0.updated < start }
        addHistorySection("Today", today)
        addHistorySection("Older", older)
    }

    private func addHistorySection(_ title: String, _ rows: [Thread]) {
        guard !rows.isEmpty else { return }
        let heading = NSTextField(labelWithString: title)
        heading.font = .systemFont(ofSize: 11, weight: .medium)
        heading.textColor = YarnColor.slate
        history.addArrangedSubview(heading)
        heading.leadingAnchor.constraint(equalTo: history.leadingAnchor, constant: 12).isActive = true
        for thread in rows {
            let row = HistoryRow(title: Self.threadTitle(thread), chosen: thread.id == activeID)
            row.identifier = NSUserInterfaceItemIdentifier(thread.id)
            row.target = self
            row.action = #selector(pickChat(_:))
            history.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: history.widthAnchor).isActive = true
            row.heightAnchor.constraint(equalToConstant: 28).isActive = true
        }
    }

    private static func threadTitle(_ thread: Thread) -> String {
        let question = thread.turns.first { $0.role == "you" }?.text ?? ""
        let flat = question.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return flat.isEmpty ? "New chat" : flat
    }

    func windowDidResize(_ notification: Notification) {
        let width = scroller.contentView.bounds.width
        guard !busy, abs(width - laidOutWidth) > 1 else { return }
        render(scroll: false)
    }

    private func plain(_ text: String, size: CGFloat, color: NSColor) -> NSAttributedString {
        let style = NSMutableParagraphStyle()
        style.lineSpacing = 2
        return NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: size),
            .foregroundColor: color,
            .paragraphStyle: style,
        ])
    }

    /// Models write *like this* and **like this** when they want emphasis.
    /// Show both as bold, and turn a real bullet into a bullet.
    private func yarnText(_ raw: String) -> NSAttributedString {
        let result = NSMutableAttributedString()
        let lines = raw.replacingOccurrences(of: "\r\n", with: "\n")
            .split(separator: "\n", omittingEmptySubsequences: false)
        for (index, lineSub) in lines.enumerated() {
            if index > 0 {
                result.append(yarnInline("\n", heading: false))
            }
            var line = String(lineSub)
            if let range = line.range(of: #"^\s{0,3}#{1,3}\s+"#, options: .regularExpression) {
                line.removeSubrange(range)
                result.append(yarnInline(line, heading: true))
                continue
            }
            if let range = line.range(of: #"^\s*[-*]\s+"#, options: .regularExpression) {
                line = "• " + String(line[range.upperBound...])
            }
            let heading = line.range(of: #"^\*\*.+\*\*$"#, options: .regularExpression) != nil
            result.append(yarnInline(line, heading: heading))
        }
        return result
    }

    private func yarnInline(_ text: String, heading: Bool) -> NSAttributedString {
        let style = NSMutableParagraphStyle()
        style.lineSpacing = 3
        style.paragraphSpacing = 8
        let base = Self.chatFont(weight: heading ? .semibold : .regular)
        let emph = Self.chatFont(weight: .bold)
        let code = NSFont.monospacedSystemFont(ofSize: Self.chatSize, weight: .regular)
        let result = NSMutableAttributedString()
        let chars = Array(text)
        var index = 0
        func emit(_ slice: ArraySlice<Character>, font: NSFont) {
            guard !slice.isEmpty else { return }
            result.append(NSAttributedString(string: String(slice), attributes: [
                .font: font,
                .foregroundColor: YarnColor.ink,
                .paragraphStyle: style,
            ]))
        }
        while index < chars.count {
            if chars[index] == "`",
               let end = chars[(index + 1)...].firstIndex(of: "`"),
               end > index + 1 {
                emit(chars[(index + 1)..<end], font: code)
                index = end + 1
                continue
            }
            if chars[index] == "*" {
                let double = index + 1 < chars.count && chars[index + 1] == "*"
                let marker = double ? 2 : 1
                let start = index + marker
                var end = start
                var found = false
                while end < chars.count {
                    if double {
                        if end + 1 < chars.count && chars[end] == "*" && chars[end + 1] == "*" {
                            found = true
                            break
                        }
                    } else if chars[end] == "*" && !(end + 1 < chars.count && chars[end + 1] == "*") {
                        found = true
                        break
                    }
                    end += 1
                }
                if found && end > start {
                    emit(chars[start..<end], font: emph)
                    index = end + marker
                    continue
                }
            }
            var next = index
            while next < chars.count && chars[next] != "*" && chars[next] != "`" {
                next += 1
            }
            if next == index { next += 1 }
            emit(chars[index..<next], font: base)
            index = next
        }
        return result
    }

    private func bubble(_ text: String, mine: Bool) -> NSView {
        let available = max(scroller.contentView.bounds.width, 640)
        let textWidth = mine ? min(520, available * 0.72) : available * 0.92
        let attributed = mine
            ? plain(text, size: Self.chatSize, color: .white)
            : yarnText(text)
        let label = BubbleTextView(attributed: attributed, wrapWidth: textWidth)
        label.translatesAutoresizingMaskIntoConstraints = false

        let chip = BubbleBackdrop(mine: mine)
        chip.translatesAutoresizingMaskIntoConstraints = false
        chip.addSubview(label)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: chip.topAnchor, constant: 10),
            label.bottomAnchor.constraint(equalTo: chip.bottomAnchor, constant: -10),
            label.leadingAnchor.constraint(equalTo: chip.leadingAnchor, constant: 14),
            label.trailingAnchor.constraint(equalTo: chip.trailingAnchor, constant: -14),
            label.widthAnchor.constraint(equalToConstant: textWidth),
            chip.widthAnchor.constraint(lessThanOrEqualToConstant: textWidth + 28),
        ])

        let row = NSView()
        row.translatesAutoresizingMaskIntoConstraints = false
        row.addSubview(chip)
        NSLayoutConstraint.activate([
            chip.topAnchor.constraint(equalTo: row.topAnchor),
            chip.bottomAnchor.constraint(equalTo: row.bottomAnchor),
        ])
        if mine {
            chip.trailingAnchor.constraint(equalTo: row.trailingAnchor).isActive = true
            chip.leadingAnchor.constraint(greaterThanOrEqualTo: row.leadingAnchor, constant: 40).isActive = true
        } else {
            chip.leadingAnchor.constraint(equalTo: row.leadingAnchor).isActive = true
            chip.trailingAnchor.constraint(lessThanOrEqualTo: row.trailingAnchor, constant: -40).isActive = true
        }
        return row
    }

    private func scrollToEnd() {
        guard let doc = scroller.documentView else { return }
        let height = doc.fittingSize.height
        let visible = scroller.contentView.bounds.height
        let y = max(0, height - visible)
        scroller.contentView.scroll(to: NSPoint(x: 0, y: y))
        scroller.reflectScrolledClipView(scroller.contentView)
    }

    private static let chatSize: CGFloat = 13

    private static func chatFont(weight: NSFont.Weight = .regular) -> NSFont {
        .systemFont(ofSize: chatSize, weight: weight)
    }
}

private struct Thread {
    var id: String
    var updated: TimeInterval
    var turns: [(role: String, text: String)]
}

private final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

private final class FlippedClipView: NSClipView {
    override var isFlipped: Bool { true }
}

private final class HistoryRow: NSButton {
    private var hovering = false
    let chosen: Bool

    init(title: String, chosen: Bool) {
        self.chosen = chosen
        super.init(frame: .zero)
        self.title = title
        isBordered = false
        wantsLayer = true
        font = .systemFont(ofSize: 13, weight: chosen ? .semibold : .regular)
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
        if chosen || hovering {
            yarn(0x167DFF, chosen ? 0.22 : 0.10).setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 6, dy: 1), xRadius: 8, yRadius: 8).fill()
        }
        let shown = title as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font ?? NSFont.systemFont(ofSize: 13),
            .foregroundColor: YarnColor.ink,
        ]
        let size = shown.size(withAttributes: attrs)
        shown.draw(
            with: NSRect(x: 14, y: (bounds.height - size.height) / 2, width: bounds.width - 24, height: size.height),
            options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine],
            attributes: attrs
        )
    }
}

/// A growing plain-text box. Return asks. Shift-return, and the Mac's
/// option-return, start a new paragraph. The old single-line field treated
/// every return as "done", so shift-return asked as well.
final class AskComposer: NSView, NSTextViewDelegate {
    let textView: NSTextView
    private let scroll = NSScrollView()
    private let placeholder = PassTextField(labelWithString: "Ask about your meetings")
    private var focused = false
    private var settingText = false
    private let minHeight: CGFloat = 40
    private let maxHeight: CGFloat = 132
    private var heightConstraint: NSLayoutConstraint!
    var onSubmit: (() -> Void)?
    var onRecall: ((Int) -> String?)?
    var onEdit: ((String) -> Void)?

    var text: String { textView.string }

    override init(frame frameRect: NSRect) {
        let container = NSTextContainer(size: NSSize(width: 240, height: CGFloat.greatestFiniteMagnitude))
        container.widthTracksTextView = true
        container.heightTracksTextView = false
        container.lineFragmentPadding = 0
        let layout = NSLayoutManager()
        layout.addTextContainer(container)
        let storage = NSTextStorage()
        storage.addLayoutManager(layout)
        textView = NSTextView(frame: .zero, textContainer: container)
        super.init(frame: frameRect)
        wantsLayer = true
        translatesAutoresizingMaskIntoConstraints = false
        heightConstraint = heightAnchor.constraint(equalToConstant: minHeight)
        heightConstraint.isActive = true
        setContentHuggingPriority(.required, for: .vertical)
        setContentCompressionResistancePriority(.required, for: .vertical)

        textView.delegate = self
        textView.isRichText = false
        textView.importsGraphics = false
        textView.isEditable = true
        textView.isSelectable = true
        textView.allowsUndo = true
        textView.drawsBackground = false
        textView.focusRingType = .none
        textView.font = .systemFont(ofSize: 14)
        textView.textColor = YarnColor.ink
        textView.insertionPointColor = YarnColor.blue
        textView.textContainerInset = NSSize(width: 8, height: 11)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.minSize = .zero
        textView.isContinuousSpellCheckingEnabled = true
        textView.isAutomaticSpellingCorrectionEnabled = true
        textView.isGrammarCheckingEnabled = false
        textView.typingAttributes = [
            .font: NSFont.systemFont(ofSize: 14),
            .foregroundColor: YarnColor.ink,
        ]

        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.contentView = ComposerClip()
        scroll.documentView = textView
        scroll.translatesAutoresizingMaskIntoConstraints = false
        addSubview(scroll)

        placeholder.font = .systemFont(ofSize: 14)
        placeholder.textColor = YarnColor.slate
        placeholder.translatesAutoresizingMaskIntoConstraints = false
        placeholder.isBezeled = false
        placeholder.drawsBackground = false
        placeholder.isEditable = false
        placeholder.isSelectable = false
        placeholder.lineBreakMode = .byTruncatingTail
        addSubview(placeholder)

        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: topAnchor),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
            placeholder.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 12),
            placeholder.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            placeholder.topAnchor.constraint(equalTo: topAnchor, constant: 11),
        ])
    }

    required init?(coder: NSCoder) { nil }

    func setText(_ value: String) {
        settingText = true
        textView.string = value
        let end = (value as NSString).length
        textView.setSelectedRange(NSRange(location: end, length: 0))
        settingText = false
        refreshChrome()
        onEdit?(value)
    }

    func textDidChange(_ notification: Notification) {
        refreshChrome()
        textView.scrollRangeToVisible(textView.selectedRange())
        if !settingText { onEdit?(textView.string) }
    }

    override func layout() {
        super.layout()
        let width = scroll.contentSize.width
        if width > 40, abs(textView.frame.width - width) > 0.5 {
            textView.frame.size.width = width
        }
        refreshHeight()
    }

    private func refreshChrome() {
        placeholder.isHidden = !textView.string.isEmpty
        refreshHeight()
    }

    private func refreshHeight() {
        guard bounds.width > 40, let layout = textView.layoutManager, let container = textView.textContainer else {
            return
        }
        layout.ensureLayout(for: container)
        let used = layout.usedRect(for: container).height + textView.textContainerInset.height * 2
        let next = min(maxHeight, max(minHeight, ceil(used)))
        guard abs(heightConstraint.constant - next) > 0.5 else { return }
        heightConstraint.constant = next
    }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.cornerRadius = 10
        layer?.borderWidth = 1
        let border = focused ? YarnColor.blue : YarnColor.slate.withAlphaComponent(0.45)
        layer?.borderColor = border.cgColor
        layer?.backgroundColor = YarnColor.canvas.cgColor
    }

    func textDidBeginEditing(_ notification: Notification) {
        focused = true
        needsDisplay = true
    }

    func textDidEndEditing(_ notification: Notification) {
        focused = false
        needsDisplay = true
    }

    func textView(_ textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        // A return still inside a Japanese or Chinese composition belongs to
        // the input method, not to Ask.
        if textView.hasMarkedText() { return false }
        let flags = NSApp.currentEvent?.modifierFlags ?? []
        let newline = commandSelector == #selector(NSResponder.insertNewline(_:))
            || commandSelector == #selector(NSResponder.insertNewlineIgnoringFieldEditor(_:))
            || commandSelector == #selector(NSResponder.insertLineBreak(_:))
        if newline {
            let breakLine = flags.contains(.shift) || flags.contains(.option)
                || commandSelector != #selector(NSResponder.insertNewline(_:))
            if breakLine {
                textView.insertText("\n", replacementRange: textView.selectedRange())
            } else {
                onSubmit?()
            }
            return true
        }
        if commandSelector == #selector(NSResponder.moveUp(_:)) && caret(onFirstLine: true),
           let next = onRecall?(-1) {
            setText(next)
            return true
        }
        if commandSelector == #selector(NSResponder.moveDown(_:)) && caret(onFirstLine: false),
           let next = onRecall?(1) {
            setText(next)
            return true
        }
        if commandSelector == #selector(NSResponder.insertTab(_:))
            || commandSelector == #selector(NSResponder.insertBacktab(_:)) {
            return true
        }
        if commandSelector == #selector(NSResponder.cancelOperation(_:)), !textView.string.isEmpty {
            setText("")
            return true
        }
        return false
    }

    /// True on the first line, false on the last. Arrow recall only takes
    /// over at those edges, so a paragraph can still be edited.
    private func caret(onFirstLine first: Bool) -> Bool {
        let ns = textView.string as NSString
        let loc = min(textView.selectedRange().location, ns.length)
        let slice = first ? ns.substring(to: loc) : ns.substring(from: loc)
        return !slice.contains("\n")
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(textView)
    }
}

private final class ComposerClip: NSClipView {
    override var isFlipped: Bool { true }
}

private final class PassTextField: NSTextField {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

private final class AskButton: NSButton {
    override func draw(_ dirtyRect: NSRect) {
        yarn(0x167DFF, isEnabled ? 1 : 0.45).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
        let title = "Ask" as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 14, weight: .semibold),
            .foregroundColor: NSColor.white,
        ]
        let size = title.size(withAttributes: attrs)
        title.draw(
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

/// Rich chat text you can select and copy. ``NSTextField`` drops bold and
/// bullets when the field editor opens on click; a read-only text view keeps them.
private final class BubbleTextView: NSTextView {
    private let wrapWidth: CGFloat

    init(attributed: NSAttributedString, wrapWidth: CGFloat) {
        self.wrapWidth = wrapWidth
        let container = NSTextContainer(size: NSSize(width: wrapWidth, height: 1.0e7))
        container.widthTracksTextView = false
        container.lineFragmentPadding = 0
        let layout = NSLayoutManager()
        let storage = NSTextStorage()
        layout.addTextContainer(container)
        storage.addLayoutManager(layout)
        super.init(frame: .zero, textContainer: container)
        storage.setAttributedString(attributed)
        isEditable = false
        isSelectable = true
        isRichText = true
        drawsBackground = false
        backgroundColor = .clear
        isVerticallyResizable = true
        isHorizontallyResizable = false
        textContainerInset = .zero
        maxSize = NSSize(width: wrapWidth, height: 1.0e7)
        minSize = NSSize(width: wrapWidth, height: 0)
    }

    required init?(coder: NSCoder) { nil }

    override var intrinsicContentSize: NSSize {
        guard let layout = layoutManager, let container = textContainer else {
            return NSSize(width: wrapWidth, height: 1)
        }
        layout.ensureLayout(for: container)
        let used = layout.usedRect(for: container)
        return NSSize(width: wrapWidth, height: max(1, ceil(used.height)))
    }
}

private final class BubbleBackdrop: NSView {
    let mine: Bool

    init(mine: Bool) {
        self.mine = mine
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 12
    }

    required init?(coder: NSCoder) { nil }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.backgroundColor = (mine ? YarnColor.blue : YarnColor.cloud).cgColor
    }
}
