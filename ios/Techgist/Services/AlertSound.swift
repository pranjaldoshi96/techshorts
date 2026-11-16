import AVFoundation
import UserNotifications

/// The sound an alert makes. The reader picks one in Settings; the push server is told the choice with the
/// registration, and the app's own notifications use it too. Files are made by `scripts/make_sounds.sh` and
/// `scripts/make_chime.sh`.
enum AlertSound: String, CaseIterable, Identifiable {
    case flash, wire, chime, soft, system = "default"

    static let key = "alertSound"
    static var current: AlertSound {
        UserDefaults.standard.string(forKey: key).flatMap(AlertSound.init(rawValue:)) ?? .flash
    }

    var id: String { rawValue }

    var title: String {
        switch self {
        case .flash: "News flash"
        case .wire: "News wire"
        case .chime: "Chime"
        case .soft: "Soft"
        case .system: "iPhone default"
        }
    }

    /// The file in the app bundle; nil for the iPhone's own sound.
    var fileName: String? {
        switch self {
        case .flash: "techgist-flash.caf"
        case .wire: "techgist-wire.caf"
        case .chime: "techgist.caf"
        case .soft: "techgist-soft.caf"
        case .system: nil
        }
    }

    var notificationSound: UNNotificationSound {
        fileName.map { UNNotificationSound(named: UNNotificationSoundName($0)) } ?? .default
    }

    private static var player: AVAudioPlayer?

    /// Plays the sound once, so the reader hears what they picked. The iPhone's default sound has no file to play.
    func preview() {
        guard let fileName, let url = Bundle.main.url(forResource: fileName, withExtension: nil) else { return }
        try? AVAudioSession.sharedInstance().setCategory(.ambient)
        Self.player = try? AVAudioPlayer(contentsOf: url)
        Self.player?.play()
    }
}
