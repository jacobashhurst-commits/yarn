// One system-wide shortcut for record and stop: Control-Option-Command-R.
//
// Off unless turned on in Settings. A recording still starts only because a
// person pressed something; this is a second button, not a trigger. Three
// modifier keys at once, so it cannot be typed by accident in another app.
//
// Carbon's hot key call is the one that needs no Accessibility or Input
// Monitoring permission. The newer event-tap route would ask for the right
// to watch every key pressed on the Mac, which Yarn has no business holding.

import AppKit
import Carbon.HIToolbox

final class YarnHotKey {
    static let shared = YarnHotKey()
    /// "off", "room" or "call": what the shortcut starts when nothing is recording.
    private(set) var mode = "off"
    var onPress: ((String) -> Void)?

    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?

    /// Safe to call every second with the current setting; it only acts on a change.
    func apply(_ wanted: String) {
        let next = (wanted == "room" || wanted == "call") ? wanted : "off"
        if next == mode { return }
        mode = next
        if next == "off" {
            unregister()
        } else if hotKey == nil {
            register()
        }
    }

    private func register() {
        if handler == nil {
            var spec = EventTypeSpec(
                eventClass: OSType(kEventClassKeyboard),
                eventKind: UInt32(kEventHotKeyPressed)
            )
            let me = Unmanaged.passUnretained(self).toOpaque()
            InstallEventHandler(
                GetApplicationEventTarget(),
                { _, _, userData in
                    guard let userData else { return noErr }
                    let this = Unmanaged<YarnHotKey>.fromOpaque(userData).takeUnretainedValue()
                    DispatchQueue.main.async { this.onPress?(this.mode) }
                    return noErr
                },
                1,
                &spec,
                me,
                &handler
            )
        }
        // "YARN" as a four-character code. Only has to be unique within this app.
        let id = EventHotKeyID(signature: OSType(0x5941_524E), id: 1)
        let status = RegisterEventHotKey(
            UInt32(kVK_ANSI_R),
            UInt32(controlKey | optionKey | cmdKey),
            id,
            GetApplicationEventTarget(),
            0,
            &hotKey
        )
        if status != noErr {
            // Another app already holds this combination. Nothing to fall
            // back to; the buttons still work and Settings shows it as on.
            hotKey = nil
        }
    }

    private func unregister() {
        if let hotKey { UnregisterEventHotKey(hotKey) }
        hotKey = nil
    }
}
