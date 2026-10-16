// The welcome shown at the end of Setup, and again when Yarn is opened,
// until the person ticks Don't show again.

import AppKit

enum YarnWelcomeSettings {
    static func isHidden(_ path: String) -> Bool {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["hide_welcome"] as? Bool == true
    }

    static func hide(_ path: String) {
        write(path, "hide_welcome", true)
        write(path, "welcome_handoff", 0)
    }

    /// Setup just showed the welcome and opened Yarn. Skip the copy that
    /// would otherwise appear on top of it a second later.
    static func handoff(_ path: String) {
        write(path, "welcome_handoff", Date().timeIntervalSince1970)
    }

    static func consumeHandoff(_ path: String) -> Bool {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let stamp = obj["welcome_handoff"] as? Double
        else { return false }
        write(path, "welcome_handoff", 0)
        return Date().timeIntervalSince1970 - stamp < 120
    }

    private static func write(_ path: String, _ key: String, _ value: Any) {
        let url = URL(fileURLWithPath: path)
        var data: [String: Any] = [:]
        if let raw = try? Data(contentsOf: url),
           let obj = try? JSONSerialization.jsonObject(with: raw) as? [String: Any] {
            data = obj
        }
        data[key] = value
        if let out = try? JSONSerialization.data(withJSONObject: data, options: [.prettyPrinted, .sortedKeys]) {
            try? FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try? out.write(to: url)
        }
    }
}

final class YarnWelcomePane: NSView {
    let settingsPath: String
    var onStart: () -> Void
    let scroll = NSScrollView()
    let skip = NSButton(checkboxWithTitle: "Don't show again", target: nil, action: nil)
    private let document = FlippedDocument()
    private let stack = NSStackView()
    private var snapped = false

    init(settingsPath: String, onStart: @escaping () -> Void) {
        self.settingsPath = settingsPath
        self.onStart = onStart
        super.init(frame: .zero)
        build()
    }

    required init?(coder: NSCoder) { nil }

    func rememberChoice() {
        if skip.state == .on {
            YarnWelcomeSettings.hide(settingsPath)
        }
    }

    override func layout() {
        super.layout()
        let width = scroll.contentView.bounds.width
        guard width > 1 else { return }
        stack.frame.size.width = max(0, width - 72)
        stack.layoutSubtreeIfNeeded()
        let fit = stack.fittingSize.height
        guard fit > 1 else { return }
        let height = fit + 20
        document.frame = NSRect(x: 0, y: 0, width: width, height: height)
        stack.frame = NSRect(x: 36, y: 8, width: width - 72, height: fit)
        guard !snapped else { return }
        scroll.contentView.scroll(to: .zero)
        scroll.reflectScrolledClipView(scroll.contentView)
        snapped = true
    }

    private func build() {
        let ink = welcomeColor(light: 0x19293D, dark: 0xE7EEF6)
        let navy = welcomeColor(light: 0x0D244B, dark: 0xD6E4F5)
        let slate = welcomeColor(light: 0x617188, dark: 0x9AAFC3)
        let blue = welcomeColor(light: 0x167DFF, dark: 0x7AADFF)

        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        scroll.documentView = document
        scroll.translatesAutoresizingMaskIntoConstraints = false
        addSubview(scroll)

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        document.addSubview(stack)

        let mark = NSImageView()
        if let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png") {
            mark.image = NSImage(contentsOf: url)
        }
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.translatesAutoresizingMaskIntoConstraints = false
        mark.widthAnchor.constraint(equalToConstant: 92).isActive = true
        mark.heightAnchor.constraint(equalToConstant: 46).isActive = true
        stack.addArrangedSubview(mark)

        addText("Welcome to Yarn", size: 28, weight: .semibold, color: navy)
        addText("Your meetings. Your memories. Your computer.", size: 16, weight: .medium, color: blue)
        stack.setCustomSpacing(18, after: stack.arrangedSubviews.last!)
        addText(
            "Yarn records your meetings, figures out who said what, and turns conversations into searchable memories, actionable notes and a daily brief.",
            size: 14, weight: .regular, color: ink
        )
        addText(
            "Everything is processed and stored locally on your Mac by default. No cloud required, no subscriptions, no one else storing your conversations.",
            size: 14, weight: .regular, color: ink
        )
        stack.setCustomSpacing(22, after: stack.arrangedSubviews.last!)
        addText("How does it work?", size: 18, weight: .semibold, color: navy)
        addReason(
            "Small brain. Surprisingly useful. (Like its creator.)",
            "Yarn uses free, local AI. It doesn't have the horsepower of ChatGPT or Claude, but you don't need a Ferrari to drive to the shops.",
            navy: navy, slate: slate
        )
        addReason(
            "It's a little slower. Who cares?",
            "Yarn works in the background. Record your next meeting while it's processing the last one.",
            navy: navy, slate: slate
        )
        addReason(
            "Keep transcripts, not terabytes.",
            "Text takes almost no space. Automatically purge audio after processing, a week later, or keep everything. Your call.",
            navy: navy, slate: slate
        )
        addReason(
            "Your morning, sorted.",
            "A daily brief of important discussions, outstanding actions and what needs your attention.",
            navy: navy, slate: slate
        )
        stack.setCustomSpacing(22, after: stack.arrangedSubviews.last!)
        addText("Getting started", size: 18, weight: .semibold, color: navy)
        addStep("1", "Record", "Click the little Yarn icon hiding in your Mac's menu bar.", navy: navy, slate: slate, blue: blue)
        addStep("2", "Get on with it", "Yarn transcribes and summarises in the background.", navy: navy, slate: slate, blue: blue)
        addStep("3", "Remember", "Search your meetings or open your daily brief. All locally.", navy: navy, slate: slate, blue: blue)
        stack.setCustomSpacing(18, after: stack.arrangedSubviews.last!)
        addText(
            "Yarn isn't trying to be ChatGPT or Claude. It doesn't need to be. (But if you want a bigger brain, Yarn plays nicely with both through MCP.)",
            size: 14, weight: .regular, color: slate
        )
        addText(
            "Not everything needs to be a SaaS. Your conversations are yours. It's nice to keep them that way.",
            size: 14, weight: .medium, color: ink
        )

        skip.font = NSFont.systemFont(ofSize: 13)
        skip.translatesAutoresizingMaskIntoConstraints = false
        addSubview(skip)

        let go = WelcomeButton(title: "Get started →")
        go.target = self
        go.action = #selector(start)
        go.keyEquivalent = "\r"
        go.translatesAutoresizingMaskIntoConstraints = false
        addSubview(go)

        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: topAnchor, constant: 12),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: go.topAnchor, constant: -12),

            skip.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 36),
            skip.centerYAnchor.constraint(equalTo: go.centerYAnchor),

            go.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -36),
            go.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -20),
            go.widthAnchor.constraint(equalToConstant: 160),
            go.heightAnchor.constraint(equalToConstant: 40),
        ])
    }

    @objc private func start() {
        rememberChoice()
        onStart()
    }

    private func addText(_ text: String, size: CGFloat, weight: NSFont.Weight, color: NSColor) {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = NSFont.systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.maximumNumberOfLines = 0
        field.preferredMaxLayoutWidth = 520
        stack.addArrangedSubview(field)
    }

    private func addReason(_ title: String, _ body: String, navy: NSColor, slate: NSColor) {
        let block = NSStackView()
        block.orientation = .vertical
        block.alignment = .leading
        block.spacing = 4
        let heading = NSTextField(wrappingLabelWithString: title)
        heading.font = NSFont.systemFont(ofSize: 15, weight: .semibold)
        heading.textColor = navy
        heading.maximumNumberOfLines = 0
        heading.preferredMaxLayoutWidth = 520
        let copy = NSTextField(wrappingLabelWithString: body)
        copy.font = NSFont.systemFont(ofSize: 14)
        copy.textColor = slate
        copy.maximumNumberOfLines = 0
        copy.preferredMaxLayoutWidth = 520
        block.addArrangedSubview(heading)
        block.addArrangedSubview(copy)
        stack.addArrangedSubview(block)
    }

    private func addStep(_ number: String, _ title: String, _ body: String, navy: NSColor, slate: NSColor, blue: NSColor) {
        let n = NSTextField(labelWithString: number)
        n.font = NSFont.systemFont(ofSize: 15, weight: .semibold)
        n.textColor = blue
        n.translatesAutoresizingMaskIntoConstraints = false
        n.widthAnchor.constraint(equalToConstant: 22).isActive = true
        let words = NSStackView()
        words.orientation = .vertical
        words.alignment = .leading
        words.spacing = 2
        let heading = NSTextField(wrappingLabelWithString: title)
        heading.font = NSFont.systemFont(ofSize: 15, weight: .semibold)
        heading.textColor = navy
        heading.maximumNumberOfLines = 0
        heading.preferredMaxLayoutWidth = 490
        let copy = NSTextField(wrappingLabelWithString: body)
        copy.font = NSFont.systemFont(ofSize: 14)
        copy.textColor = slate
        copy.maximumNumberOfLines = 0
        copy.preferredMaxLayoutWidth = 490
        words.addArrangedSubview(heading)
        words.addArrangedSubview(copy)
        let row = NSStackView(views: [n, words])
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = 8
        stack.addArrangedSubview(row)
    }
}

final class YarnWelcomeWindow: NSWindowController, NSWindowDelegate {
    let pane: YarnWelcomePane

    init(settingsPath: String, onStart: @escaping () -> Void) {
        pane = YarnWelcomePane(settingsPath: settingsPath, onStart: onStart)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 640, height: 760),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Welcome to Yarn"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = welcomeColor(light: 0xFFFFFF, dark: 0x101820)
        window.contentView = pane
        window.center()
        super.init(window: window)
        window.delegate = self
    }

    required init?(coder: NSCoder) { nil }

    func windowWillClose(_ notification: Notification) {
        pane.rememberChoice()
    }
}

private final class FlippedDocument: NSView {
    override var isFlipped: Bool { true }
}

func welcomeColor(light: UInt32, dark: UInt32) -> NSColor {
    NSColor(name: nil) { appearance in
        let hex = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        return NSColor(
            srgbRed: CGFloat((hex >> 16) & 0xff) / 255,
            green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255,
            alpha: 1
        )
    }
}

private final class WelcomeButton: NSButton {
    convenience init(title: String) {
        self.init(frame: .zero)
        self.title = title
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 8
    }

    override func draw(_ dirtyRect: NSRect) {
        welcomeColor(light: 0x167DFF, dark: 0x167DFF).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
        let shown = title as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 15, weight: .semibold),
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
