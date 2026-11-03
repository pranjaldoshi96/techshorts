import Foundation

/// Small JSON-on-disk persistence. Application Support is backed up; Caches is not.
enum Disk {
    static func url(_ name: String, in directory: FileManager.SearchPathDirectory) -> URL {
        let base = FileManager.default.urls(for: directory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base.appendingPathComponent(name)
    }

    static func load<T: Decodable>(_ type: T.Type, from name: String,
                                   in directory: FileManager.SearchPathDirectory = .applicationSupportDirectory) -> T? {
        guard let data = try? Data(contentsOf: url(name, in: directory)) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    static func save<T: Encodable>(_ value: T, to name: String,
                                   in directory: FileManager.SearchPathDirectory = .applicationSupportDirectory) {
        guard let data = try? JSONEncoder().encode(value) else { return }
        try? data.write(to: url(name, in: directory), options: .atomic)
    }
}
