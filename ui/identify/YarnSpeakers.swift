// Yarn "Identify speakers" window.
//
//   YarnSpeakers --ma /path/to/ma <meeting_id>
//
// A real window instead of `display dialog`, because every AppleScript dialog
// button dismisses the dialog: Play clip could never keep it on screen. Here
// Play, Next clip and Stop leave the window exactly where it is, the text
// under the player is the transcript of the clip being played, and the name
// box is pre-filled only with a predicted name (or left empty).
//
// Built by bin/speaker-popup into an .app and launched with `open`, so it
// belongs to the login session rather than whatever process spawned it.

import AppKit
import AVFoundation

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
}

func yarnMark(height: CGFloat) -> NSImageView {
    let view = NSImageView()
    if let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png") {
        view.image = NSImage(contentsOf: url)
    }
    view.imageScaling = .scaleProportionallyUpOrDown
    view.translatesAutoresizingMaskIntoConstraints = false
    view.widthAnchor.constraint(equalToConstant: height * 2).isActive = true
    view.heightAnchor.constraint(equalToConstant: height).isActive = true
    return view
}

final class YarnButton: NSButton {
    convenience init(title: String) {
        self.init(frame: .zero)
        self.title = title
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 8
    }

    override func draw(_ dirtyRect: NSRect) {
        yarn(0x167DFF, isEnabled ? 1 : 0.45).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 8, yRadius: 8).fill()
        let shown = title as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
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

struct Clip: Decodable {
    let start: Double
    let duration: Double
    let text: String
    let exact: Bool
    let at: String
}

struct Match: Decodable {
    let name: String
    let score: Double
}

struct Voice: Decodable {
    let key: String
    let label: String
    let detail: String
    let named: String
    let guess: String
    let reason: String
    let speaking_s: Double?
    let turns: Int?
    let crumb: Bool?
    let matches: [Match]
    let clips: [Clip]

    var isCrumb: Bool { crumb ?? false }
}

struct Person: Decodable {
    let name: String
    let title: String
    let company: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        company = try c.decodeIfPresent(String.self, forKey: .company) ?? ""
    }

    private enum CodingKeys: String, CodingKey {
        case name, title, company
    }
}

struct NameChoice {
    let name: String
    let title: String
    let company: String

    /// What the menu shows. Title and company are how two people who share a
    /// first name are told apart before one of them is selected.
    var label: String {
        let role = [title, company].filter { !$0.isEmpty }.joined(separator: ", ")
        if role.isEmpty { return name }
        return "\(name) — \(role)"
    }

    var showsRole: Bool { label != name }
}

struct Payload: Decodable {
    let meeting_id: String
    let banner: String
    let has_audio: Bool
    let names: [String]
    let people: [Person]
    let voices: [Voice]
}

func runMA(_ ma: String, _ args: [String]) -> (Int32, String, String) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: ma)
    process.arguments = args
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    process.environment = env
    let out = Pipe()
    let err = Pipe()
    process.standardOutput = out
    process.standardError = err
    do {
        try process.run()
    } catch {
        return (127, "", "could not run \(ma): \(error)")
    }
    let outData = out.fileHandleForReading.readDataToEndOfFile()
    let errData = err.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return (
        process.terminationStatus,
        String(data: outData, encoding: .utf8) ?? "",
        String(data: errData, encoding: .utf8) ?? ""
    )
}

/// Wrapping label whose height follows the width it was actually given.
/// A fixed preferredMaxLayoutWidth wraps the text on screen but reports the
/// height of a much wider line, so the next view draws on top of it.
final class WrapLabel: NSTextField {
    init(size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor = .labelColor) {
        super.init(frame: .zero)
        isEditable = false
        isBezeled = false
        drawsBackground = false
        isSelectable = true
        font = NSFont.systemFont(ofSize: size, weight: weight)
        textColor = color
        usesSingleLineMode = false
        maximumNumberOfLines = 0
        cell?.wraps = true
        cell?.isScrollable = false
        lineBreakMode = .byWordWrapping
        preferredMaxLayoutWidth = 460
        setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        setContentHuggingPriority(.required, for: .vertical)
        setContentCompressionResistancePriority(.required, for: .vertical)
        translatesAutoresizingMaskIntoConstraints = false
    }

    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        let width = bounds.width
        guard width > 1, abs(preferredMaxLayoutWidth - width) > 0.5 else { return }
        preferredMaxLayoutWidth = width
        invalidateIntrinsicContentSize()
    }

    override var intrinsicContentSize: NSSize {
        let width = preferredMaxLayoutWidth > 1 ? preferredMaxLayoutWidth : 460
        guard let cell else { return super.intrinsicContentSize }
        cell.wraps = true
        let size = cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: width, height: 10_000))
        return NSSize(width: NSView.noIntrinsicMetric, height: max(ceil(size.height), font?.pointSize ?? 13))
    }

    func show(_ text: String, spacing: CGFloat = 2) {
        attributedStringValue = NSAttributedString(string: text, attributes: textAttributes(spacing: spacing))
        isHidden = text.isEmpty
        invalidateIntrinsicContentSize()
    }

    func show(attributed text: NSAttributedString) {
        attributedStringValue = text
        isHidden = text.length == 0
        invalidateIntrinsicContentSize()
    }

    private func textAttributes(spacing: CGFloat) -> [NSAttributedString.Key: Any] {
        let style = NSMutableParagraphStyle()
        style.lineBreakMode = .byWordWrapping
        style.lineSpacing = spacing
        return [
            .font: font as Any,
            .foregroundColor: textColor as Any,
            .paragraphStyle: style,
        ]
    }
}

final class CardView: NSView {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { fatalError() }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.cornerRadius = 12
        layer?.borderWidth = 0
        layer?.backgroundColor = YarnColor.cloud.cgColor
    }
}

final class Controller: NSObject, NSWindowDelegate, AVAudioPlayerDelegate {
    let ma: String
    let meetingID: String
    var payload: Payload!
    var queue: [Voice] = []
    var reviewMode = false
    var position = 0
    var clipIndex = 0
    var answers: [String: String] = [:]
    var clipCache: [String: URL] = [:]
    var player: AVAudioPlayer?
    var finished = false

    let window: NSWindow
    let dateLabel = WrapLabel(size: 12, color: YarnColor.slate)
    let titleLabel = WrapLabel(size: 22, weight: .semibold, color: YarnColor.navy)
    let metaLabel = WrapLabel(size: 12, color: YarnColor.slate)
    let voiceLabel = NSTextField(labelWithString: "")
    let progressLabel = NSTextField(labelWithString: "")
    let detailLabel = WrapLabel(size: 12, color: YarnColor.slate)
    let guessLabel = WrapLabel(size: 18, weight: .semibold, color: YarnColor.ink)
    let roleLabel = WrapLabel(size: 13, color: YarnColor.slate)
    let captionLabel = WrapLabel(size: 12, color: YarnColor.slate)
    let matchesLabel = WrapLabel(size: 12, color: YarnColor.slate)
    let savedLabel = WrapLabel(size: 12, color: YarnColor.slate)
    let clipHeader = WrapLabel(size: 12, color: YarnColor.slate)
    let clipText = WrapLabel(size: 15, color: YarnColor.ink)
    let statusLabel = NSTextField(labelWithString: "")
    let nameBox = NSComboBox()
    let titleBox = NSTextField()
    let companyBox = NSTextField()
    let lastNameBox = NSTextField()
    let distinguishHint = WrapLabel(size: 13, weight: .regular, color: YarnColor.slate)
    var titles: [String: String] = [:]
    var companies: [String: String] = [:]
    var choices: [NameChoice] = []
    var applyingChoice = false
    var distinctKeys: Set<String> = []
    var askedToDistinguish = false
    var showingVoice = false
    let playButton = NSButton(title: "Play", target: nil, action: nil)
    let nextButton = NSButton(title: "Next clip", target: nil, action: nil)
    let stopButton = NSButton(title: "Stop", target: nil, action: nil)
    let backButton = NSButton(title: "Back", target: nil, action: nil)
    let skipButton = NSButton(title: "Skip, don't know", target: nil, action: nil)
    let quitButton = NSButton(title: "Stop asking", target: nil, action: nil)
    let saveButton = YarnButton(title: "Save & next")
    var formViews: [NSView] = []
    var formStack: NSStackView!
    var didFit = false

    init(ma: String, meetingID: String) {
        self.ma = ma
        self.meetingID = meetingID
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 530),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        window.title = "Identify speakers"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = YarnColor.canvas
        window.level = .floating
        window.delegate = self
        window.contentMinSize = NSSize(width: 460, height: 480)
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        buildUI()
    }

    func buildUI() {
        for (button, action, symbol) in [
            (playButton, #selector(play), "play.fill"),
            (nextButton, #selector(nextClip), "forward.fill"),
            (stopButton, #selector(stopClip), "stop.fill"),
        ] as [(NSButton, Selector, String)] {
            button.target = self
            button.action = action
            button.bezelStyle = .rounded
            button.imagePosition = .imageLeading
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
                .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 11, weight: .semibold))
        }
        for (button, action) in [
            (backButton, #selector(back)),
            (skipButton, #selector(skip)),
            (quitButton, #selector(quit)),
            (saveButton, #selector(save)),
        ] {
            button.target = self
            button.action = action
            button.bezelStyle = .rounded
        }
        saveButton.keyEquivalent = "\r"
        quitButton.keyEquivalent = "\u{1b}"

        voiceLabel.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        voiceLabel.textColor = YarnColor.navy
        voiceLabel.lineBreakMode = .byTruncatingTail
        voiceLabel.maximumNumberOfLines = 1
        voiceLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        voiceLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        progressLabel.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        progressLabel.textColor = YarnColor.slate
        progressLabel.alignment = .right
        progressLabel.setContentHuggingPriority(.required, for: .horizontal)
        progressLabel.setContentCompressionResistancePriority(.required, for: .horizontal)

        statusLabel.font = NSFont.systemFont(ofSize: 12)
        statusLabel.textColor = YarnColor.slate
        statusLabel.alignment = .right
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.setContentHuggingPriority(.required, for: .horizontal)
        statusLabel.setContentCompressionResistancePriority(.required, for: .horizontal)

        nameBox.isEditable = true
        nameBox.completes = true
        nameBox.numberOfVisibleItems = 14
        nameBox.controlSize = .large
        nameBox.placeholderString = "Name, or leave blank if you don't know"
        nameBox.font = NSFont.systemFont(ofSize: 15)
        nameBox.target = self
        nameBox.action = #selector(namePicked)
        titleBox.translatesAutoresizingMaskIntoConstraints = false
        companyBox.translatesAutoresizingMaskIntoConstraints = false
        lastNameBox.translatesAutoresizingMaskIntoConstraints = false
        titleBox.placeholderString = "Title, optional. Finance director"
        titleBox.font = NSFont.systemFont(ofSize: 14)
        titleBox.controlSize = .large
        companyBox.placeholderString = "Company, optional. Northwind"
        companyBox.font = NSFont.systemFont(ofSize: 14)
        companyBox.controlSize = .large
        lastNameBox.placeholderString = "Last name"
        lastNameBox.font = NSFont.systemFont(ofSize: 14)
        lastNameBox.controlSize = .large
        lastNameBox.isHidden = true
        distinguishHint.isHidden = true

        let pad: CGFloat = 22

        let voiceSpacer = NSView()
        voiceSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let voiceRow = NSStackView(views: [voiceLabel, voiceSpacer, progressLabel])
        voiceRow.orientation = .horizontal
        voiceRow.alignment = .firstBaseline
        voiceRow.spacing = 12

        let prediction = NSStackView(views: [guessLabel, roleLabel, captionLabel, matchesLabel, savedLabel])
        prediction.orientation = .vertical
        prediction.alignment = .leading
        prediction.spacing = 3
        prediction.detachesHiddenViews = true

        let clipStack = NSStackView(views: [clipHeader, clipText])
        clipStack.orientation = .vertical
        clipStack.alignment = .leading
        clipStack.spacing = 8
        clipStack.translatesAutoresizingMaskIntoConstraints = false
        let card = CardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(clipStack)
        NSLayoutConstraint.activate([
            clipStack.topAnchor.constraint(equalTo: card.topAnchor, constant: 12),
            clipStack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -14),
            clipStack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 14),
            clipStack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -14),
            clipHeader.widthAnchor.constraint(equalTo: clipStack.widthAnchor),
            clipText.widthAnchor.constraint(equalTo: clipStack.widthAnchor),
        ])

        let playerSpacer = NSView()
        playerSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        playerSpacer.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let playerRow = NSStackView(views: [playButton, nextButton, stopButton, playerSpacer, statusLabel])
        playerRow.orientation = .horizontal
        playerRow.alignment = .centerY
        playerRow.spacing = 8

        let nameLabel = NSTextField(labelWithString: "Who is this?")
        nameLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        nameLabel.textColor = YarnColor.navy
        saveButton.translatesAutoresizingMaskIntoConstraints = false
        saveButton.heightAnchor.constraint(equalToConstant: 30).isActive = true
        saveButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 112).isActive = true
        let nameStack = NSStackView(views: [
            nameLabel, nameBox, titleBox, companyBox, distinguishHint, lastNameBox,
        ])
        nameStack.orientation = .vertical
        nameStack.alignment = .leading
        nameStack.spacing = 6

        let mark = yarnMark(height: 28)
        let column: [NSView] = [
            mark, dateLabel, titleLabel, metaLabel, voiceRow, detailLabel,
            prediction, card, playerRow, nameStack,
        ]
        let stack = NSStackView(views: column)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.edgeInsets = NSEdgeInsets(top: 18, left: pad, bottom: 16, right: pad)
        stack.detachesHiddenViews = true
        stack.setCustomSpacing(8, after: mark)
        stack.setCustomSpacing(2, after: dateLabel)
        stack.setCustomSpacing(4, after: titleLabel)
        stack.setCustomSpacing(18, after: metaLabel)
        stack.setCustomSpacing(6, after: voiceRow)
        stack.setCustomSpacing(14, after: detailLabel)
        stack.setCustomSpacing(16, after: prediction)
        stack.setCustomSpacing(14, after: card)
        stack.setCustomSpacing(16, after: playerRow)
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.setContentHuggingPriority(.required, for: .vertical)
        stack.setContentCompressionResistancePriority(.required, for: .vertical)

        let fullWidth: [NSView] = [dateLabel, titleLabel, metaLabel, voiceRow, detailLabel, prediction, card, playerRow, nameStack]
        for view in fullWidth {
            view.translatesAutoresizingMaskIntoConstraints = false
            view.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -(pad * 2)).isActive = true
        }
        for label in [guessLabel, roleLabel, captionLabel, matchesLabel, savedLabel] {
            label.widthAnchor.constraint(equalTo: prediction.widthAnchor).isActive = true
        }
        nameBox.widthAnchor.constraint(equalTo: nameStack.widthAnchor).isActive = true
        titleBox.widthAnchor.constraint(equalTo: nameStack.widthAnchor).isActive = true
        companyBox.widthAnchor.constraint(equalTo: nameStack.widthAnchor).isActive = true
        lastNameBox.widthAnchor.constraint(equalTo: nameStack.widthAnchor).isActive = true
        distinguishHint.widthAnchor.constraint(equalTo: nameStack.widthAnchor).isActive = true

        formViews = [voiceRow, detailLabel, prediction, card, playerRow, nameStack]

        formStack = stack
        let document = NSView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: document.topAnchor),
            stack.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            document.bottomAnchor.constraint(greaterThanOrEqualTo: stack.bottomAnchor),
        ])

        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.automaticallyAdjustsContentInsets = false
        scroll.scrollerStyle = .overlay
        scroll.documentView = document

        let rule = NSBox()
        rule.boxType = .separator
        rule.translatesAutoresizingMaskIntoConstraints = false

        let barSpacer = NSView()
        barSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        barSpacer.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let bottomRow = NSStackView(views: [quitButton, backButton, barSpacer, skipButton, saveButton])
        bottomRow.orientation = .horizontal
        bottomRow.alignment = .centerY
        bottomRow.spacing = 8
        bottomRow.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        content.addSubview(scroll)
        content.addSubview(rule)
        content.addSubview(bottomRow)
        let documentFillsClip = document.heightAnchor.constraint(equalTo: scroll.contentView.heightAnchor)
        documentFillsClip.priority = NSLayoutConstraint.Priority(rawValue: 50)
        NSLayoutConstraint.activate([
            document.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            document.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor),
            documentFillsClip,

            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: content.topAnchor),

            rule.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            rule.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            rule.topAnchor.constraint(equalTo: scroll.bottomAnchor),
            rule.heightAnchor.constraint(equalToConstant: 1),

            bottomRow.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: pad),
            bottomRow.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -pad),
            bottomRow.topAnchor.constraint(equalTo: rule.bottomAnchor, constant: 12),
            bottomRow.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),
        ])
        window.contentView = content
    }

    func start() {
        titleLabel.show("Loading voices…")
        dateLabel.isHidden = true
        metaLabel.isHidden = true
        setForm(shown: false)
        window.center()
        var origin = window.frame.origin
        let step = CGFloat(meetingID.utf8.reduce(0) { ($0 + Int($1)) % 5 })
        origin.x += step * 26
        origin.y -= step * 22
        window.setFrameOrigin(origin)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        setControls(enabled: false)
        DispatchQueue.global(qos: .userInitiated).async {
            let (code, out, err) = runMA(self.ma, ["speakers", self.meetingID, "--identify-json"])
            DispatchQueue.main.async {
                guard code == 0, let data = out.data(using: .utf8),
                      let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
                    self.fail("Could not load this meeting.\n\(err)")
                    return
                }
                self.loaded(payload)
            }
        }
    }

    func loaded(_ payload: Payload) {
        self.payload = payload
        applyBanner(payload.banner)
        buildChoices()
        // A voice under ten seconds is not a person to name.
        let unnamed = payload.voices.filter { $0.named.isEmpty }
        let meaningful = unnamed.filter { !$0.isCrumb }
        queue = meaningful.isEmpty ? unnamed : meaningful
        if queue.isEmpty {
            reviewMode = true
            queue = payload.voices.filter { !$0.isCrumb }
            if queue.isEmpty { queue = payload.voices }
        }
        if queue.isEmpty {
            fail("No separate voices were detected in this meeting.")
            return
        }
        setForm(shown: true)
        setControls(enabled: true)
        show(0)
    }

    func applyBanner(_ raw: String) {
        let lines = raw.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var date = ""
        var title = ""
        var meta: [String] = []
        for (index, line) in lines.enumerated() where !line.isEmpty {
            if index == 0 {
                date = line
            } else if line.hasPrefix("Your notes:") || line.hasPrefix("No notes") || line.hasPrefix("Who was there:") {
                meta.append(line)
            } else if title.isEmpty {
                title = line
            } else {
                meta.append(line)
            }
        }
        dateLabel.show(date)
        titleLabel.show(title.isEmpty ? "Identify speakers" : title, spacing: 1)
        metaLabel.show(meta.joined(separator: "\n"), spacing: 3)
        window.title = title.isEmpty ? "Identify speakers" : title
        window.subtitle = "Identify speakers"
    }

    func fail(_ message: String) {
        titleLabel.show("Could not open this meeting")
        metaLabel.show(message)
        setForm(shown: false)
        setControls(enabled: false)
        quitButton.isEnabled = true
        quitButton.title = "Close"
    }

    func setForm(shown: Bool) {
        for view in formViews {
            view.isHidden = !shown
        }
    }

    func setControls(enabled: Bool) {
        for button in [playButton, nextButton, stopButton, backButton, skipButton, saveButton] {
            button.isEnabled = enabled
        }
        nameBox.isEnabled = enabled
        titleBox.isEnabled = enabled
        companyBox.isEnabled = enabled
        lastNameBox.isEnabled = enabled
    }

    var voice: Voice { queue[position] }

    func show(_ index: Int) {
        stopClip()
        showingVoice = true
        position = index
        clipIndex = 0
        let v = voice
        voiceLabel.stringValue = v.label
        if reviewMode {
            progressLabel.stringValue = "Reviewing \(index + 1) of \(queue.count)"
        } else {
            progressLabel.stringValue = "\(index + 1) of \(queue.count)"
        }
        detailLabel.show(v.detail, spacing: 1)
        applyPrediction(v)

        askedToDistinguish = false
        lastNameBox.stringValue = ""
        lastNameBox.isHidden = true
        distinguishHint.isHidden = true
        let answer = answers[v.key]
        let shownName = answer ?? (v.named.isEmpty ? v.guess : v.named)
        nameBox.stringValue = shownName
        if titles[v.key] == nil {
            let person = uniquePerson(shownName)
            titles[v.key] = person?.title ?? ""
            companies[v.key] = person?.company ?? ""
        }
        titleBox.stringValue = titles[v.key] ?? ""
        companyBox.stringValue = companies[v.key] ?? ""
        backButton.isEnabled = index > 0
        saveButton.title = index == queue.count - 1 ? "Save & finish" : "Save & next"
        renderClip()
        window.makeFirstResponder(nameBox)
        showingVoice = false
        DispatchQueue.main.async { self.fitWindow() }
    }

    func fitWindow() {
        window.layoutIfNeeded()
        guard !didFit else { return }
        let form = formStack.bounds.height
        guard form > 120 else { return }
        didFit = true
        let screen = (window.screen?.visibleFrame.height ?? 900) - 48
        let target = min(max(form + 64, 460), screen)
        let content = window.contentRect(forFrameRect: window.frame)
        let delta = target - content.height
        guard abs(delta) > 2 else { return }
        var frame = window.frame
        frame.size.height += delta
        frame.origin.y -= delta
        window.setFrame(frame, display: true)
    }

    func applyPrediction(_ v: Voice) {
        if v.guess.isEmpty {
            guessLabel.font = NSFont.systemFont(ofSize: 15, weight: .medium)
            guessLabel.textColor = YarnColor.slate
            guessLabel.show("No confident match")
        } else {
            guessLabel.font = NSFont.systemFont(ofSize: 18, weight: .semibold)
            guessLabel.textColor = YarnColor.ink
            guessLabel.show(v.guess, spacing: 0)
        }
        let who = v.guess.isEmpty ? v.named : v.guess
        roleLabel.show(roleLine(who), spacing: 0)
        captionLabel.show(caption(for: v), spacing: 1)
        matchesLabel.show(attributed: matchesLine(v.matches))
        savedLabel.show(v.named.isEmpty ? "" : "Saved as \(v.named)")
    }

    func caption(for v: Voice) -> String {
        let reason = v.reason
        let score = v.matches.first.map { String(format: "%.2f", $0.score) }
        if reason.contains("strong voice match"), let score {
            return "Strong voice match · \(score)"
        }
        if reason.contains("voice match"), let score {
            return "Possible voice match · \(score)"
        }
        if reason.hasPrefix("named in the conversation") {
            return "Named in the conversation"
        }
        if reason == "no confident voice match" {
            return "Nothing close enough in known speakers"
        }
        return reason.prefix(1).uppercased() + reason.dropFirst()
    }

    func matchesLine(_ matches: [Match]) -> NSAttributedString {
        let line = NSMutableAttributedString()
        guard !matches.isEmpty else { return line }
        let style = NSMutableParagraphStyle()
        style.lineBreakMode = .byWordWrapping
        style.lineSpacing = 2
        line.append(NSAttributedString(string: "Closest", attributes: [
            .font: NSFont.systemFont(ofSize: 12),
            .foregroundColor: NSColor.tertiaryLabelColor,
            .paragraphStyle: style,
        ]))
        for (index, match) in matches.prefix(3).enumerated() {
            line.append(NSAttributedString(string: "   ·   ", attributes: [
                .font: NSFont.systemFont(ofSize: 12),
                .foregroundColor: NSColor.tertiaryLabelColor,
                .paragraphStyle: style,
            ]))
            let leading = index == 0
            line.append(NSAttributedString(
                string: "\(match.name) \(String(format: "%.2f", match.score))",
                attributes: [
                    .font: NSFont.systemFont(ofSize: 12, weight: leading ? .semibold : .regular),
                    .foregroundColor: leading ? YarnColor.ink : YarnColor.slate,
                    .paragraphStyle: style,
                ]
            ))
        }
        return line
    }

    func renderClip() {
        let clips = voice.clips
        guard !clips.isEmpty else {
            clipHeader.show("No clip available")
            clipText.show("")
            playButton.isEnabled = false
            nextButton.isEnabled = false
            return
        }
        let clip = clips[clipIndex]
        let length = String(format: "%.1f sec", clip.duration)
        var header = "Clip \(clipIndex + 1) of \(clips.count)   ·   \(clip.at)   ·   \(length)"
        header += clip.exact ? "   ·   only this voice" : "   ·   approximate"
        clipHeader.show(header, spacing: 1)
        if clip.text.isEmpty {
            clipText.textColor = YarnColor.slate
            clipText.show("No transcript for this stretch. Play it to hear them.")
        } else {
            clipText.textColor = YarnColor.ink
            let prefix = clip.exact ? "" : "Roughly: "
            clipText.show("\(prefix)“\(clip.text)”", spacing: 3)
        }
        playButton.isEnabled = payload.has_audio
        nextButton.isEnabled = payload.has_audio && clips.count > 1
        if !payload.has_audio {
            statusLabel.stringValue = "Audio has been deleted"
        }
    }

    @objc func play() {
        let key = voice.key
        let index = clipIndex
        let cacheKey = "\(key)#\(index)"
        stopClip()
        if let url = clipCache[cacheKey] {
            startPlayer(url)
            return
        }
        statusLabel.stringValue = "Cutting clip…"
        DispatchQueue.global(qos: .userInitiated).async {
            let (code, out, _) = runMA(self.ma, [
                "speakers", self.meetingID, "--clip-path", key, "--clip-index", String(index),
            ])
            DispatchQueue.main.async {
                let path = out.trimmingCharacters(in: .whitespacesAndNewlines)
                guard code == 0, !path.isEmpty else {
                    self.statusLabel.stringValue = "Could not play clip"
                    return
                }
                let url = URL(fileURLWithPath: path)
                self.clipCache[cacheKey] = url
                if self.voice.key == key && self.clipIndex == index {
                    self.startPlayer(url)
                }
            }
        }
    }

    func startPlayer(_ url: URL) {
        do {
            let p = try AVAudioPlayer(contentsOf: url)
            p.delegate = self
            p.play()
            player = p
            statusLabel.stringValue = "Playing…"
            statusLabel.textColor = YarnColor.ink
        } catch {
            statusLabel.stringValue = "Could not play clip"
        }
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        statusLabel.stringValue = "Finished"
        statusLabel.textColor = YarnColor.slate
    }

    @objc func nextClip() {
        let count = voice.clips.count
        guard count > 0 else { return }
        clipIndex = (clipIndex + 1) % count
        renderClip()
        play()
    }

    @objc func stopClip() {
        player?.stop()
        player = nil
        if statusLabel.stringValue == "Playing…" || statusLabel.stringValue == "Finished" {
            statusLabel.stringValue = ""
        }
        statusLabel.textColor = YarnColor.slate
    }

    func buildChoices() {
        choices = []
        var seen = Set<String>()
        for person in payload.people {
            let choice = NameChoice(name: person.name, title: person.title, company: person.company)
            let key = fold(choice.label)
            guard !key.isEmpty, !seen.contains(key) else { continue }
            seen.insert(key)
            choices.append(choice)
        }
        for name in payload.names {
            let key = fold(name)
            guard !key.isEmpty else { continue }
            if choices.contains(where: { fold($0.name) == key }) { continue }
            choices.append(NameChoice(name: name, title: "", company: ""))
        }
        nameBox.removeAllItems()
        nameBox.addItems(withObjectValues: choices.map(\.label))
    }

    func record() {
        let typed = nameBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if let picked = choices.first(where: { $0.showsRole && $0.label == typed }) {
            answers[voice.key] = picked.name
            titles[voice.key] = picked.title
            companies[voice.key] = picked.company
            nameBox.stringValue = picked.name
            titleBox.stringValue = picked.title
            companyBox.stringValue = picked.company
            return
        }
        answers[voice.key] = typed
        titles[voice.key] = titleBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        companies[voice.key] = companyBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func knownPerson(_ name: String) -> Person? {
        let key = fold(name)
        guard !key.isEmpty else { return nil }
        return payload?.people.first { fold($0.name) == key }
    }

    /// One saved person with this name. Two Jacobs stay unresolved until the
    /// menu row that includes a title or company is chosen.
    func uniquePerson(_ name: String) -> Person? {
        let key = fold(name)
        guard !key.isEmpty else { return nil }
        let matches = payload?.people.filter { fold($0.name) == key } ?? []
        return matches.count == 1 ? matches[0] : nil
    }

    func roleLine(_ name: String) -> String {
        guard let person = uniquePerson(name) else { return "" }
        return [person.title, person.company].filter { !$0.isEmpty }.joined(separator: ", ")
    }

    @objc func namePicked() {
        if showingVoice || applyingChoice { return }
        let typed = nameBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if let picked = choices.first(where: { $0.showsRole && $0.label == typed }) {
            applyingChoice = true
            nameBox.stringValue = picked.name
            titleBox.stringValue = picked.title
            companyBox.stringValue = picked.company
            applyingChoice = false
            record()
            return
        }
        if let person = uniquePerson(typed) {
            if titleBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                titleBox.stringValue = person.title
            }
            if companyBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                companyBox.stringValue = person.company
            }
        }
        record()
    }

    func fold(_ value: String) -> String {
        value.lowercased().split { $0.isWhitespace }.joined(separator: " ")
    }

    /// jacob and Jacob are one person. A second Jacob, with no last name,
    /// title or company, is asked to add one. Accepting the suggested name
    /// is not a second person.
    func needsDistinction(_ name: String, title: String, company: String) -> Bool {
        let key = fold(name)
        guard !key.isEmpty, !key.contains(" "), title.isEmpty, company.isEmpty else { return false }
        if fold(voice.guess) == key || fold(voice.named) == key { return false }
        let known = payload?.people.contains { fold($0.name) == key || fold($0.name).split(separator: " ").first.map(String.init) == key } ?? false
        let other = answers.contains { other, value in
            other != voice.key && (fold(value) == key || fold(value).split(separator: " ").first.map(String.init) == key)
        }
        return known || other
    }

    func canonical(_ name: String) -> String {
        let key = fold(name)
        if let person = payload?.people.first(where: { fold($0.name) == key }) {
            return person.name
        }
        return name
    }

    @objc func save() {
        record()
        var name = answers[voice.key] ?? ""
        let title = titles[voice.key] ?? ""
        let company = companies[voice.key] ?? ""
        if !lastNameBox.isHidden {
            let last = lastNameBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if !last.isEmpty {
                let first = canonical(name).split { $0.isWhitespace }.first.map(String.init) ?? name
                name = "\(first) \(last)"
                answers[voice.key] = name
            } else {
                name = canonical(name)
                answers[voice.key] = name
            }
        }
        if needsDistinction(name, title: title, company: company) && !askedToDistinguish {
            askedToDistinguish = true
            let known = canonical(name)
            distinguishHint.show(
                "Yarn already knows \(known). Add a last name, a title or a company if this is someone else. Save again if this is the same \(known)."
            )
            lastNameBox.isHidden = false
            window.makeFirstResponder(lastNameBox)
            return
        }
        if askedToDistinguish && (!lastNameBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !title.isEmpty || !company.isEmpty) {
            distinctKeys.insert(voice.key)
        }
        if lastNameBox.isHidden {
            answers[voice.key] = canonical(name)
        }
        // One confirmation covers every other voice that already guessed the
        // same person - diarize often splits Charles into Charles×4.
        propagateSameName()
        advance()
    }

    @objc func skip() {
        answers[voice.key] = voice.named.isEmpty ? "" : voice.named
        advance()
    }

    @objc func back() {
        record()
        if position > 0 { show(position - 1) }
    }

    func propagateSameName() {
        let name = (answers[voice.key] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        var applied = 0
        for v in queue where v.key != voice.key {
            if answers[v.key] != nil { continue }
            let guess = v.guess.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !guess.isEmpty else { continue }
            if guess.caseInsensitiveCompare(name) == .orderedSame {
                answers[v.key] = name
                if (titles[v.key] ?? "").isEmpty {
                    titles[v.key] = titles[voice.key] ?? ""
                    companies[v.key] = companies[voice.key] ?? ""
                }
                applied += 1
            }
        }
        if applied > 0 {
            statusLabel.stringValue = applied == 1
                ? "Also named 1 other \(name) voice"
                : "Also named \(applied) other \(name) voices"
            statusLabel.textColor = YarnColor.slate
        }
    }

    func advance() {
        var next = position + 1
        while next < queue.count, answers[queue[next].key] != nil {
            next += 1
        }
        if next < queue.count {
            show(next)
        } else {
            finish()
        }
    }

    @objc func quit() {
        if payload != nil && !queue.isEmpty { record() }
        finish()
    }

    /// The worker prints internal keys such as room-1. The window says Speaker.
    func savedLines(_ lines: [String]) -> String {
        lines.map { line in
            var shown = line
            for voice in queue {
                let prefix = voice.key + " ->"
                if shown.hasPrefix(prefix) {
                    shown = voice.label + " →" + shown.dropFirst(prefix.count)
                    break
                }
            }
            return shown
        }.joined(separator: "\n")
    }

    func finish() {
        guard !finished else { return }
        finished = true
        stopClip()
        var args = ["speakers", meetingID]
        if payload != nil {
            for v in queue {
                guard let answer = answers[v.key], !answer.isEmpty, answer != v.named else { continue }
                args += ["--set", "\(v.key)=\(answer)"]
                if let title = titles[v.key], !title.isEmpty {
                    args += ["--title", "\(v.key)=\(title)"]
                }
                if let company = companies[v.key], !company.isEmpty {
                    args += ["--company", "\(v.key)=\(company)"]
                }
                if distinctKeys.contains(v.key) {
                    args += ["--distinct", v.key]
                }
            }
        }
        guard args.count > 2 else {
            NSApp.terminate(nil)
            return
        }
        setControls(enabled: false)
        statusLabel.stringValue = "Saving names…"
        DispatchQueue.global(qos: .userInitiated).async {
            let (code, out, err) = runMA(self.ma, args)
            DispatchQueue.main.async {
                if code == 0 {
                    let lines = out.split(separator: "\n").filter {
                        !$0.hasPrefix("ASSUMED") && !$0.hasPrefix("run `ma")
                    }
                    showYarnResult(
                        title: "Speaker names saved",
                        body: self.savedLines(lines.map(String.init)),
                        footer: "Names land in the background. If one person shows up twice, Yarn merges them."
                    )
                } else {
                    showYarnResult(
                        title: "Could not save speaker names",
                        body: err,
                        footer: ""
                    )
                }
            }
        }
    }

    func windowWillClose(_ notification: Notification) {
        if payload != nil && !queue.isEmpty && !finished { record() }
        finish()
    }
}

final class ResultCloser: NSObject {
    static let shared = ResultCloser()
    @objc func close() { NSApp.terminate(nil) }
}

func showYarnResult(title: String, body: String, footer: String) {
    for window in NSApp.windows {
        window.orderOut(nil)
    }
    let panel = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 480, height: 360),
        styleMask: [.titled, .closable],
        backing: .buffered,
        defer: false
    )
    panel.title = title
    panel.titleVisibility = .hidden
    panel.titlebarAppearsTransparent = true
    panel.backgroundColor = YarnColor.canvas
    panel.isReleasedWhenClosed = false

    let mark = yarnMark(height: 36)
    let heading = NSTextField(labelWithString: title)
    heading.font = .systemFont(ofSize: 20, weight: .semibold)
    heading.textColor = YarnColor.navy
    heading.translatesAutoresizingMaskIntoConstraints = false

    let header = NSStackView(views: [mark, heading])
    header.orientation = .horizontal
    header.alignment = .centerY
    header.spacing = 12
    header.translatesAutoresizingMaskIntoConstraints = false

    let list = NSTextField(wrappingLabelWithString: body.isEmpty ? " " : body)
    list.font = .systemFont(ofSize: 14)
    list.textColor = YarnColor.ink
    list.preferredMaxLayoutWidth = 420
    list.isHidden = body.isEmpty
    list.translatesAutoresizingMaskIntoConstraints = false

    let lines = max(body.split(separator: "\n").count, body.isEmpty ? 0 : 1)
    let listHeight = min(340, CGFloat(lines) * 22 + 8)

    let scroller = NSScrollView()
    scroller.drawsBackground = false
    scroller.hasVerticalScroller = lines > 14
    scroller.borderType = .noBorder
    scroller.documentView = list
    scroller.translatesAutoresizingMaskIntoConstraints = false
    scroller.isHidden = body.isEmpty

    let note = NSTextField(wrappingLabelWithString: footer)
    note.font = .systemFont(ofSize: 13)
    note.textColor = YarnColor.slate
    note.preferredMaxLayoutWidth = 420
    note.isHidden = footer.isEmpty
    note.translatesAutoresizingMaskIntoConstraints = false

    let ok = YarnButton(title: "OK")
    ok.target = ResultCloser.shared
    ok.action = #selector(ResultCloser.close)
    ok.keyEquivalent = "\r"
    ok.translatesAutoresizingMaskIntoConstraints = false

    let content = NSView()
    panel.contentView = content
    content.addSubview(header)
    content.addSubview(scroller)
    content.addSubview(note)
    content.addSubview(ok)
    NSLayoutConstraint.activate([
        list.leadingAnchor.constraint(equalTo: scroller.contentView.leadingAnchor),
        list.trailingAnchor.constraint(equalTo: scroller.contentView.trailingAnchor),
        list.topAnchor.constraint(equalTo: scroller.contentView.topAnchor),
        list.widthAnchor.constraint(equalTo: scroller.widthAnchor),

        header.topAnchor.constraint(equalTo: content.topAnchor, constant: 8),
        header.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
        header.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -20),

        scroller.topAnchor.constraint(equalTo: header.bottomAnchor, constant: body.isEmpty ? 0 : 16),
        scroller.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
        scroller.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -22),
        scroller.heightAnchor.constraint(equalToConstant: body.isEmpty ? 0 : listHeight),

        note.topAnchor.constraint(equalTo: scroller.bottomAnchor, constant: footer.isEmpty ? 0 : 14),
        note.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
        note.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -22),

        ok.topAnchor.constraint(equalTo: note.bottomAnchor, constant: 16),
        ok.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
        ok.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
        ok.widthAnchor.constraint(equalToConstant: 72),
        ok.heightAnchor.constraint(equalToConstant: 32),
    ])
    panel.center()
    panel.makeKeyAndOrderFront(nil)
    NSApp.activate(ignoringOtherApps: true)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: Controller?

    func applicationDidFinishLaunching(_ notification: Notification) {
        var args = Array(CommandLine.arguments.dropFirst())
        var ma = "/opt/homebrew/bin/ma"
        if let i = args.firstIndex(of: "--ma"), i + 1 < args.count {
            ma = args[i + 1]
            args.removeSubrange(i...(i + 1))
        }
        guard let meetingID = args.first(where: { !$0.hasPrefix("-") }) else {
            showYarnResult(title: "No meeting given", body: "", footer: "")
            return
        }
        let c = Controller(ma: ma, meetingID: meetingID)
        controller = c
        c.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
