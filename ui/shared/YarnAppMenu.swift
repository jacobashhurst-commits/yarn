// The menu bar every Yarn window shares.
//
// AppKit only delivers Cmd-C, Cmd-V, Cmd-X, Cmd-A and Cmd-Z through a main
// menu. A window with text boxes and no Edit menu cannot paste, and nothing
// says why. Notes, Identify and Setup each run as their own small app, so
// each one needs this, not only the Dock window.
//
// The first item of a main menu is always shown as the application menu,
// whatever it is titled. Putting Edit first made Edit the app menu and left
// no Quit, Hide or Close Window shortcut anywhere.

import AppKit

enum YarnAppMenu {
    /// Call once, before `app.run()`. `settings` adds "Settings…" on Cmd-comma.
    static func install(
        appName: String = "Yarn",
        settings: (target: AnyObject, action: Selector)? = nil
    ) {
        let main = NSMenu()
        main.addItem(submenu(appMenu(appName, settings: settings)))
        main.addItem(submenu(editMenu()))
        let window = windowMenu()
        main.addItem(submenu(window))
        NSApp.mainMenu = main
        NSApp.windowsMenu = window
    }

    private static func submenu(_ menu: NSMenu) -> NSMenuItem {
        let item = NSMenuItem()
        item.title = menu.title
        item.submenu = menu
        return item
    }

    private static func item(
        _ title: String,
        _ action: Selector?,
        _ key: String = "",
        _ mask: NSEvent.ModifierFlags = [.command]
    ) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
        entry.keyEquivalentModifierMask = key.isEmpty ? [] : mask
        return entry
    }

    private static func appMenu(
        _ name: String,
        settings: (target: AnyObject, action: Selector)?
    ) -> NSMenu {
        let menu = NSMenu(title: name)
        if let settings {
            let entry = item("Settings\u{2026}", settings.action, ",")
            entry.target = settings.target
            menu.addItem(entry)
            menu.addItem(.separator())
        }
        menu.addItem(item("Hide \(name)", #selector(NSApplication.hide(_:)), "h"))
        menu.addItem(item(
            "Hide Others",
            #selector(NSApplication.hideOtherApplications(_:)),
            "h",
            [.command, .option]
        ))
        menu.addItem(item("Show All", #selector(NSApplication.unhideAllApplications(_:))))
        menu.addItem(.separator())
        menu.addItem(item("Quit \(name)", #selector(NSApplication.terminate(_:)), "q"))
        return menu
    }

    /// Every action goes to the first responder, so one menu serves a text
    /// field, a combo box, a text view, and read-only text you can select.
    private static func editMenu() -> NSMenu {
        let menu = NSMenu(title: "Edit")
        menu.addItem(item("Undo", Selector(("undo:")), "z"))
        menu.addItem(item("Redo", Selector(("redo:")), "z", [.command, .shift]))
        menu.addItem(.separator())
        menu.addItem(item("Cut", #selector(NSText.cut(_:)), "x"))
        menu.addItem(item("Copy", #selector(NSText.copy(_:)), "c"))
        menu.addItem(item("Paste", #selector(NSText.paste(_:)), "v"))
        menu.addItem(item(
            "Paste and Match Style",
            #selector(NSTextView.pasteAsPlainText(_:)),
            "v",
            [.command, .option, .shift]
        ))
        menu.addItem(item("Delete", #selector(NSText.delete(_:))))
        menu.addItem(item("Select All", #selector(NSText.selectAll(_:)), "a"))
        return menu
    }

    private static func windowMenu() -> NSMenu {
        let menu = NSMenu(title: "Window")
        menu.addItem(item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"))
        menu.addItem(item("Zoom", #selector(NSWindow.performZoom(_:))))
        menu.addItem(.separator())
        menu.addItem(item("Close", #selector(NSWindow.performClose(_:)), "w"))
        return menu
    }
}
