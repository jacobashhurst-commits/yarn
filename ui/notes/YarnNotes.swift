// The window that appears when a recording stops.
//
//   YarnNotes --ma /path/to/ma <meeting_id> [--attendees "Alex, Sam"]
//
// One window, two sections: what the meeting was about, and who was in it.
// Both are optional. Not now, or Save with both boxes empty, leaves the
// meeting exactly as it was.

import AppKit

func yarn(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(
        srgbRed: CGFloat((hex >> 16) & 0xff) / 255,
        green: CGFloat((hex >> 8) & 0xff) / 255,
        blue: CGFloat(hex & 0xff) / 255,
        alpha: alpha
    )
}

func yarnAdaptive(light: UInt32, dark: UInt32) -> NSColor {
    NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? yarn(dark) : yarn(light)
    }
}

enum YarnColor {
    static let navy = yarnAdaptive(light: 0x0D244B, dark: 0xD6E4F5)
    static let ink = yarnAdaptive(light: 0x19293D, dark: 0xE7EEF6)
    static let slate = yarnAdaptive(light: 0x617188, dark: 0x9AAFC3)
    static let blue = yarn(0x167DFF)
    static let cloud = yarnAdaptive(light: 0xEDF6FF, dark: 0x172433)
    static let canvas = yarnAdaptive(light: 0xFFFFFF, dark: 0x101820)
    static let line = yarnAdaptive(light: 0xD7E3EF, dark: 0x2A3C4E)
}

final class YarnButton: NSButton {
    var filled = true

    convenience init(title: String, filled: Bool) {
        self.init(frame: .zero)
        self.title = title
        self.filled = filled
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 8
    }

    override func draw(_ dirtyRect: NSRect) {
        if filled {
            yarn(0x167DFF, isEnabled ? 1 : 0.45).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
        } else {
            YarnColor.line.setStroke()
            let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 8, yRadius: 8)
            path.lineWidth = 1
            path.stroke()
        }
        let shown = title as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
            .foregroundColor: filled ? NSColor.white : YarnColor.navy,
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

final class Controller: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let window: NSWindow
    let nameField = NSTextField(string: "")
    let peopleField = NSTextField(string: "")
    let ma: String
    let meetingID: String

    init(ma: String, meetingID: String, attendees: String) {
        self.ma = ma
        self.meetingID = meetingID
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 520),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        super.init()
        peopleField.stringValue = attendees
        build()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        window.makeFirstResponder(nameField)
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.terminate(nil)
    }

    func build() {
        window.title = "Yarn"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = YarnColor.canvas
        window.delegate = self

        let mark = NSImageView()
        if let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png") {
            mark.image = NSImage(contentsOf: url)
        }
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.translatesAutoresizingMaskIntoConstraints = false

        let word = label("Yarn", size: 13, weight: .medium, color: YarnColor.navy, mono: true)
        let heading = label("This meeting", size: 26, weight: .semibold, color: YarnColor.navy, mono: false)
        let intro = label(
            "Both are optional. Save what you know, or leave them for later.",
            size: 13,
            weight: .regular,
            color: YarnColor.slate,
            mono: false
        )
        intro.maximumNumberOfLines = 2

        let about = section(
            "What was it about?",
            "A short name is enough. You will see it when speakers are named.",
            nameField,
            placeholder: ""
        )
        let who = section(
            "Who was in it?",
            "Include yourself, and anyone on the phone or the video call.",
            peopleField,
            placeholder: "separated by comma"
        )

        let skip = YarnButton(title: "Not now", filled: false)
        skip.target = self
        skip.action = #selector(dismiss)
        skip.translatesAutoresizingMaskIntoConstraints = false
        let save = YarnButton(title: "Save", filled: true)
        save.target = self
        save.action = #selector(saveNotes)
        save.keyEquivalent = "\r"
        save.translatesAutoresizingMaskIntoConstraints = false

        let buttons = NSStackView(views: [skip, save])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        buttons.distribution = .fillEqually

        let titles = NSStackView(views: [word, heading])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 2

        let header = NSStackView(views: [mark, titles])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 12

        let root = NSStackView(views: [header, intro, about, who, buttons])
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 16
        root.edgeInsets = NSEdgeInsets(top: 28, left: 28, bottom: 24, right: 28)
        root.translatesAutoresizingMaskIntoConstraints = false
        about.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true
        who.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true
        buttons.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true

        let content = NSView()
        content.wantsLayer = true
        content.layer?.backgroundColor = YarnColor.canvas.cgColor
        window.contentView = content
        content.addSubview(root)
        NSLayoutConstraint.activate([
            mark.widthAnchor.constraint(equalToConstant: 72),
            mark.heightAnchor.constraint(equalToConstant: 36),
            skip.heightAnchor.constraint(equalToConstant: 32),
            save.heightAnchor.constraint(equalToConstant: 32),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])
    }

    func section(_ title: String, _ hint: String, _ field: NSTextField, placeholder: String) -> NSView {
        let heading = label(title, size: 15, weight: .semibold, color: YarnColor.navy, mono: false)
        let detail = label(hint, size: 13, weight: .regular, color: YarnColor.slate, mono: false)
        detail.maximumNumberOfLines = 2
        field.placeholderString = placeholder
        field.font = NSFont.systemFont(ofSize: 15)
        field.textColor = YarnColor.ink
        field.focusRingType = .none
        field.bezelStyle = .roundedBezel
        field.translatesAutoresizingMaskIntoConstraints = false
        field.heightAnchor.constraint(equalToConstant: 28).isActive = true

        let card = NSView()
        card.wantsLayer = true
        card.layer?.backgroundColor = YarnColor.cloud.cgColor
        card.layer?.cornerRadius = 12
        let stack = NSStackView(views: [heading, detail, field])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.edgeInsets = NSEdgeInsets(top: 14, left: 14, bottom: 14, right: 14)
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)
        NSLayoutConstraint.activate([
            field.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor),
            stack.topAnchor.constraint(equalTo: card.topAnchor),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor),
        ])
        return card
    }

    func label(_ text: String, size: CGFloat, weight: NSFont.Weight, color: NSColor, mono: Bool) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = mono
            ? NSFont.monospacedSystemFont(ofSize: size, weight: weight)
            : NSFont.systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.maximumNumberOfLines = 1
        return field
    }

    @objc func dismiss() {
        NSApp.terminate(nil)
    }

    @objc func saveNotes() {
        let notes = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let people = peopleField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if notes.isEmpty && people.isEmpty {
            NSApp.terminate(nil)
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: ma)
        process.arguments = ["notes", meetingID, "--notes", notes, "--attendees", people]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
        process.waitUntilExit()
        NSApp.terminate(nil)
    }
}

@main
enum YarnNotesApp {
    static func main() {
        let args = CommandLine.arguments
        var ma = ""
        var attendees = ""
        var meeting = ""
        var index = 1
        while index < args.count {
            let arg = args[index]
            if arg == "--ma", index + 1 < args.count {
                ma = args[index + 1]
                index += 2
            } else if arg == "--attendees", index + 1 < args.count {
                attendees = args[index + 1]
                index += 2
            } else if meeting.isEmpty {
                meeting = arg
                index += 1
            } else {
                index += 1
            }
        }
        let app = NSApplication.shared
        let delegate = Controller(ma: ma, meetingID: meeting, attendees: attendees)
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
