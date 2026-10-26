// Yarn's own menu bar icon. The items are the same script the old SwiftBar
// plugin printed. Yarn draws them, so SwiftBar does not have to be running.

import AppKit

final class YarnStatus: NSObject, NSMenuDelegate {
    static let shared = YarnStatus()

    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private var refreshing = false
    private var menuOpen = false
    private var started = false

    func start() {
        if started { return }
        started = true
        menu.delegate = self
        item.menu = menu
        item.button?.imagePosition = .imageLeading
        applyIcon()
        item.button?.title = ""
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        refresh()
    }

    func menuWillOpen(_ menu: NSMenu) {
        menuOpen = true
    }

    func menuDidClose(_ menu: NSMenu) {
        menuOpen = false
    }

    private func scriptPath() -> String? {
        let home = NSHomeDirectory()
        let candidates = [
            home + "/Library/Application Support/Yarn/menu.sh",
            home + "/.swiftbar-plugins/meeting.5s.sh",
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func applyIcon() {
        let home = NSHomeDirectory()
        let candidates = [
            home + "/Library/Application Support/Yarn/menu-mark.png",
            Bundle.main.path(forResource: "yarn-mark", ofType: "png") ?? "",
        ]
        guard let path = candidates.first(where: { !$0.isEmpty && FileManager.default.fileExists(atPath: $0) }),
              let image = NSImage(contentsOfFile: path) else {
            item.button?.title = "Yarn"
            return
        }
        image.size = NSSize(width: 30, height: 14)
        image.isTemplate = true
        item.button?.image = image
    }

    private func refresh() {
        if menuOpen || refreshing { return }
        guard let script = scriptPath() else {
            showFallback()
            return
        }
        refreshing = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let text = Self.run(script)
            DispatchQueue.main.async {
                self?.refreshing = false
                self?.apply(text)
            }
        }
    }

    private func showFallback() {
        item.button?.title = ""
        menu.removeAllItems()
        let open = NSMenuItem(title: "Open Yarn", action: #selector(openYarn), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
    }

    @objc private func openYarn() {
        NSApp.activate(ignoringOtherApps: true)
        for window in NSApp.windows where window.canBecomeMain {
            window.makeKeyAndOrderFront(nil)
            return
        }
    }

    private func apply(_ text: String?) {
        guard let text, !text.isEmpty else {
            showFallback()
            return
        }
        if menuOpen { return }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var body = lines
        if let first = body.first {
            body.removeFirst()
            let parts = first.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false)
            let title = parts.first.map { String($0).trimmingCharacters(in: .whitespaces) } ?? ""
            item.button?.title = title == "Yarn" ? "" : title
        }
        menu.removeAllItems()
        var lastTop: NSMenuItem?
        for line in body {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed == "---" {
                menu.addItem(.separator())
                lastTop = nil
                continue
            }
            if trimmed.isEmpty { continue }
            let child = trimmed.hasPrefix("-- ")
            let raw = child ? String(trimmed.dropFirst(3)) : trimmed
            let entry = MenuLine(raw)
            let menuItem = NSMenuItem(title: entry.title, action: nil, keyEquivalent: "")
            if entry.disabled {
                menuItem.isEnabled = false
            } else if entry.command != nil {
                menuItem.target = self
                menuItem.action = #selector(runItem(_:))
                menuItem.representedObject = entry
            }
            menuItem.state = entry.checked ? .on : .off
            if child {
                let parent = lastTop ?? {
                    let holder = NSMenuItem(title: entry.title, action: nil, keyEquivalent: "")
                    menu.addItem(holder)
                    return holder
                }()
                if parent.submenu == nil { parent.submenu = NSMenu() }
                parent.submenu?.addItem(menuItem)
                lastTop = parent
            } else {
                menu.addItem(menuItem)
                lastTop = menuItem
            }
        }
        if menu.items.isEmpty {
            showFallback()
            return
        }
        quietLabels(menu)
    }

    private func quietLabels(_ menu: NSMenu) {
        for row in menu.items {
            if let sub = row.submenu { quietLabels(sub) }
            if row.submenu == nil && row.action == nil && !row.isSeparatorItem {
                row.isEnabled = false
            }
        }
    }

    @objc private func runItem(_ sender: NSMenuItem) {
        guard let entry = sender.representedObject as? MenuLine, let command = entry.command else { return }
        DispatchQueue.global(qos: .userInitiated).async {
            if entry.terminal {
                let line = ([command.executable] + command.arguments).map { piece in
                    "'" + piece.replacingOccurrences(of: "'", with: "'\\''") + "'"
                }.joined(separator: " ")
                let escaped = line.replacingOccurrences(of: "\\", with: "\\\\")
                    .replacingOccurrences(of: "\"", with: "\\\"")
                let source = "tell application \"Terminal\" to do script \"\(escaped)\""
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
                process.arguments = ["-e", source]
                try? process.run()
                process.waitUntilExit()
            } else {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: command.executable)
                process.arguments = command.arguments
                process.environment = Self.commandEnvironment()
                try? process.run()
                process.waitUntilExit()
            }
            if entry.refresh {
                DispatchQueue.main.async { self.refresh() }
            }
        }
    }

    /// Stop, and every other menu action, is a child of Yarn. Without Homebrew
    /// on PATH, `ma finalize` cannot see ffprobe and the recording never starts
    /// processing.
    private static func commandEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let prefix = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = prefix + ":" + (env["PATH"] ?? "")
        return env
    }

    private static func run(_ script: String) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script]
        process.environment = commandEnvironment()
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            return nil
        }
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)
    }
}

private struct MenuCommand {
    let executable: String
    let arguments: [String]
}

private struct MenuLine {
    let title: String
    let command: MenuCommand?
    let terminal: Bool
    let refresh: Bool
    let checked: Bool
    let disabled: Bool

    init(_ raw: String) {
        let parts = raw.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false)
        title = String(parts[0]).trimmingCharacters(in: .whitespaces)
        let params = parts.count > 1 ? MenuLine.parameters(String(parts[1])) : [:]
        terminal = params["terminal"] == "true"
        refresh = params["refresh"] == "true"
        checked = params["checked"] == "true"
        disabled = params["disabled"] == "true"
        if let bash = params["bash"], !bash.isEmpty {
            var args: [String] = []
            var index = 1
            while let value = params["param\(index)"] {
                args.append(value)
                index += 1
            }
            command = MenuCommand(executable: bash, arguments: args)
        } else {
            command = nil
        }
    }

    static func parameters(_ raw: String) -> [String: String] {
        var out: [String: String] = [:]
        let chars = Array(raw)
        let space: Character = " "
        let equals: Character = "="
        let single: Character = "'"
        let double: Character = "\""
        var index = 0
        func skipSpace() {
            while index < chars.count && chars[index] == space { index += 1 }
        }
        while index < chars.count {
            skipSpace()
            if index >= chars.count { break }
            var key = ""
            while index < chars.count && chars[index] != equals && chars[index] != space {
                key.append(chars[index])
                index += 1
            }
            guard index < chars.count, chars[index] == equals else { continue }
            index += 1
            var value = ""
            if index < chars.count && (chars[index] == single || chars[index] == double) {
                let quote = chars[index]
                index += 1
                while index < chars.count && chars[index] != quote {
                    value.append(chars[index])
                    index += 1
                }
                if index < chars.count { index += 1 }
            } else {
                while index < chars.count && chars[index] != space {
                    value.append(chars[index])
                    index += 1
                }
            }
            if !key.isEmpty { out[key] = value }
        }
        return out
    }
}
