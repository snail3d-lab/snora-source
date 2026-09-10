import SwiftUI

/// Photos tab → Create tab hand-off: "USE FOR VIDEO" rides the image across here.
/// RootView watches it to switch tabs; CreateView consumes it on appear.
@MainActor
final class PhotoBridge: ObservableObject {
    static let shared = PhotoBridge()
    @Published var pendingReference: UIImage?
}