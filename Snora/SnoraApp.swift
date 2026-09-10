import SwiftUI

@main
struct SnoraApp: App {
    // the user's phone, the user's farm — token baked in, no gate
    @StateObject var poller = Poller.shared
    let token = API.token

    init() {
        AssistTrace.note("── launch (pid \(ProcessInfo.processInfo.processIdentifier)) ──")
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        // one-time cleanup: an old unlock-era token in UserDefaults used to override the baked one
        UserDefaults.standard.removeObject(forKey: "token")
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(poller)
        }
    }
}

// ── root tabs live in ReelView.swift ─────────────────────────────────
