// Ask Yarn. The window is Yarn's. Ollama only writes the reply, from the
// meetings already on this Mac. History lives in asks/chat.json.

import AppKit

final class AskChat: NSObject, NSWindowDelegate, NSTextFieldDelegate {
    private let paths: Paths
    private let window: NSWindow
    private let transcript = NSStackView()
    private let scroller = NSScrollView()
    private let field = NSTextField()
    private let askButton = AskButton()
    private var saved: [(role: String, text: String)] = []
    private var pending: [(role: String, text: String)] = []
    private var busy = false

    private static var current: AskChat?

    static func show(paths: Paths) {
        if current == nil {
            current = AskChat(paths: paths)
        }
        current?.reload()
        current?.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        current?.window.makeFirstResponder(current?.field)
    }

    init(paths: Paths) {
        self.paths = paths
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 560),
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
        window.minSize = NSSize(width: 380, height: 420)
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

        field.placeholderString = "Ask about your meetings"
        field.font = .systemFont(ofSize: 14)
        field.textColor = YarnColor.ink
        field.focusRingType = .none
        field.isBezeled = true
        field.bezelStyle = .roundedBezel
        field.delegate = self
        field.translatesAutoresizingMaskIntoConstraints = false
        if let cell = field.cell as? NSTextFieldCell {
            cell.wraps = true
            cell.isScrollable = true
        }

        askButton.title = "Ask"
        askButton.isBordered = false
        askButton.target = self
        askButton.action = #selector(send)
        askButton.translatesAutoresizingMaskIntoConstraints = false

        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        askButton.setContentHuggingPriority(.required, for: .horizontal)

        let composer = NSStackView(views: [field, askButton])
        composer.orientation = .horizontal
        composer.alignment = .centerY
        composer.distribution = .fill
        composer.spacing = 8
        composer.translatesAutoresizingMaskIntoConstraints = false

        content.addSubview(header)
        content.addSubview(scroller)
        content.addSubview(composer)

        NSLayoutConstraint.activate([
            mark.widthAnchor.constraint(equalToConstant: 72),
            mark.heightAnchor.constraint(equalToConstant: 36),

            header.topAnchor.constraint(equalTo: content.topAnchor, constant: 8),
            header.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            header.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),

            scroller.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 14),
            scroller.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            scroller.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),

            composer.topAnchor.constraint(equalTo: scroller.bottomAnchor, constant: 12),
            composer.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            composer.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            composer.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),

            field.widthAnchor.constraint(greaterThanOrEqualToConstant: 180),
            field.heightAnchor.constraint(equalToConstant: 36),
            askButton.widthAnchor.constraint(equalToConstant: 72),
            askButton.heightAnchor.constraint(equalToConstant: 36),

            doc.widthAnchor.constraint(equalTo: scroller.contentView.widthAnchor),
            transcript.topAnchor.constraint(equalTo: doc.topAnchor, constant: 4),
            transcript.leadingAnchor.constraint(equalTo: doc.leadingAnchor),
            transcript.trailingAnchor.constraint(equalTo: doc.trailingAnchor),
            transcript.bottomAnchor.constraint(equalTo: doc.bottomAnchor, constant: -4),
        ])
    }

    func control(
        _ control: NSControl,
        textView: NSTextView,
        doCommandBy commandSelector: Selector
    ) -> Bool {
        if commandSelector == #selector(NSResponder.insertNewline(_:)) {
            send()
            return true
        }
        return false
    }

    @objc private func send() {
        let question = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, !busy else { return }
        busy = true
        field.stringValue = ""
        askButton.isEnabled = false
        pending = [(role: "you", text: question), (role: "yarn", text: "Looking through your meetings…")]
        render()
        let ma = paths.ma
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let reply = Self.run(ma: ma, question: question)
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                self.askButton.isEnabled = true
                if reply.ok {
                    self.pending = []
                    self.reload()
                } else {
                    self.pending = [
                        (role: "you", text: question),
                        (role: "yarn", text: reply.text),
                    ]
                    self.render()
                }
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

    private func reload() {
        saved = Self.load(paths.root + "/asks/chat.json")
        render()
    }

    private static func load(_ path: String) -> [(role: String, text: String)] {
        guard let data = FileManager.default.contents(atPath: path),
              let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
        else { return [] }
        return list.compactMap { item in
            guard let role = item["role"] as? String, let text = item["text"] as? String else {
                return nil
            }
            return (role: role, text: text)
        }
    }

    private func render() {
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
        scrollToEnd()
    }

    private func bubble(_ text: String, mine: Bool) -> NSView {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: 14)
        label.textColor = mine ? .white : YarnColor.ink
        label.preferredMaxLayoutWidth = 280
        label.maximumNumberOfLines = 0
        label.translatesAutoresizingMaskIntoConstraints = false

        let chip = BubbleBackdrop(mine: mine)
        chip.translatesAutoresizingMaskIntoConstraints = false
        chip.addSubview(label)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: chip.topAnchor, constant: 8),
            label.bottomAnchor.constraint(equalTo: chip.bottomAnchor, constant: -8),
            label.leadingAnchor.constraint(equalTo: chip.leadingAnchor, constant: 12),
            label.trailingAnchor.constraint(equalTo: chip.trailingAnchor, constant: -12),
            chip.widthAnchor.constraint(lessThanOrEqualToConstant: 320),
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
