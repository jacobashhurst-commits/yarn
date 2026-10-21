// One-shot Yarn notification. Banners use this app's icon, so the mark
// is Yarn's rather than the generic script icon osascript would show.
// An alert is a small window with the same mark.

import AppKit
import UserNotifications

final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let message: String
    let alert: Bool

    init(message: String, alert: Bool) {
        self.message = message
        self.alert = alert
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if alert || message.isEmpty {
            showWindow()
            return
        }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            DispatchQueue.main.async {
                if granted {
                    self.postBanner(center)
                } else {
                    self.showWindow()
                }
            }
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) -> UNNotificationPresentationOptions {
        [.banner, .list]
    }

    private func postBanner(_ center: UNUserNotificationCenter) {
        let content = UNMutableNotificationContent()
        content.title = "Yarn"
        content.body = message
        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        center.add(request) { _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                NSApp.terminate(nil)
            }
        }
    }

    private func showWindow() {
        NSApp.setActivationPolicy(.regular)
        let panel = NSAlert()
        panel.messageText = "Yarn"
        panel.informativeText = message.isEmpty ? " " : message
        panel.addButton(withTitle: "OK")
        if let image = mark() {
            panel.icon = image
        }
        NSApp.activate(ignoringOtherApps: true)
        panel.runModal()
        NSApp.terminate(nil)
    }

    private func mark() -> NSImage? {
        if let url = Bundle.main.url(forResource: "yarn-mark", withExtension: "png"),
           let image = NSImage(contentsOf: url) {
            return image
        }
        return NSApp.applicationIconImage
    }
}

let args = Array(CommandLine.arguments.dropFirst())
var alert = false
var words = args
if let flag = words.firstIndex(of: "--alert") {
    alert = true
    words.remove(at: flag)
}
let message = words.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)

let app = NSApplication.shared
let notifier = Notifier(message: message, alert: alert)
app.delegate = notifier
app.setActivationPolicy(.accessory)
app.run()
