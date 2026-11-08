// Автообновление: манифест latest.json и архив — файлы последнего релиза на GitHub (раньше — публичная папка Яндекс.Диска).
// Манифест подписан Ed25519 (закрытый ключ только у автора), архив проверяется по SHA-256.
// Установка всегда только после подтверждения пользователя.
import SwiftUI
import AppKit
import CryptoKit

struct UpdateManifest: Decodable {
    let version: String
    let file: String
    let sha256: String
    let size: Int
    let notes: String?
    let signature: String
}

enum UpdateError: LocalizedError {
    case msg(String)
    var errorDescription: String? { if case .msg(let s) = self { return s }; return nil }
}

func versionIsNewer(_ a: String, than b: String) -> Bool {
    let x = a.split(separator: ".").map { Int($0) ?? 0 }
    let y = b.split(separator: ".").map { Int($0) ?? 0 }
    for i in 0..<max(x.count, y.count) {
        let p = i < x.count ? x[i] : 0, q = i < y.count ? y[i] : 0
        if p != q { return p > q }
    }
    return false
}

@MainActor
final class Updater: ObservableObject {
    enum Phase: Equatable { case idle, checking, available, downloading, installing, upToDate, failed(String) }
    @Published var phase: Phase = .idle
    @Published var manifest: UpdateManifest?
    @Published var askInstall = false
    @Published var showResult = false      // показать итог ручной проверки (актуально / ошибка)
    private var manual = false

    /// Приложение запущено из карантинной папки (Загрузки): macOS исполняет его из временной копии только для чтения.
    static var isTranslocated: Bool { Bundle.main.bundlePath.contains("/AppTranslocation/") }

    private func publicDownloadURL(path: String) async throws -> URL {
        if UpdateConfig.publicURL.hasPrefix("https://github.com/") {   // файлы последнего релиза: …/releases/latest/download/<имя>
            guard let u = URL(string: UpdateConfig.publicURL + "/releases/latest/download" + path) else { throw UpdateError.msg("Bad update URL") }
            return u
        }
        var c = URLComponents(string: "https://cloud-api.yandex.net/v1/disk/public/resources/download")!
        c.queryItems = [URLQueryItem(name: "public_key", value: UpdateConfig.publicURL), URLQueryItem(name: "path", value: path)]
        let (d, r) = try await URLSession.shared.data(from: c.url!)
        guard (r as? HTTPURLResponse)?.statusCode == 200,
              let j = try JSONSerialization.jsonObject(with: d) as? [String: Any],
              let h = j["href"] as? String, let u = URL(string: h) else { throw UpdateError.msg("Update server did not return a link") }
        return u
    }

    func check(manual: Bool) {
        if phase == .checking || phase == .downloading || phase == .installing { return }
        self.manual = manual
        phase = .checking
        Task {
            do {
                let url = try await publicDownloadURL(path: "/latest.json")
                let (data, r) = try await URLSession.shared.data(from: url)
                guard (r as? HTTPURLResponse)?.statusCode == 200 else { throw UpdateError.msg("Update server returned no manifest") }
                let m = try JSONDecoder().decode(UpdateManifest.self, from: data)
                guard verify(m) else { throw UpdateError.msg("Update manifest signature is invalid") }
                if versionIsNewer(m.version, than: appVersion) {
                    manifest = m; phase = .available; askInstall = true
                } else {
                    phase = .upToDate
                    if manual { showResult = true }
                }
            } catch {
                phase = .failed(error.localizedDescription)
                if manual { showResult = true }
            }
        }
    }

    private func verify(_ m: UpdateManifest) -> Bool {
        guard let pk = Data(base64Encoded: UpdateConfig.publicKey),
              let key = try? Curve25519.Signing.PublicKey(rawRepresentation: pk),
              let sig = Data(base64Encoded: m.signature) else { return false }
        return key.isValidSignature(sig, for: Data("\(m.version)\n\(m.file)\n\(m.sha256)\n\(m.size)".utf8))
    }

    func install() {
        guard let m = manifest else { return }
        phase = .downloading
        Task {
            do {
                let fm = FileManager.default
                let work = fm.temporaryDirectory.appendingPathComponent("rtp-update-\(UUID().uuidString)")
                try fm.createDirectory(at: work, withIntermediateDirectories: true)
                let url = try await publicDownloadURL(path: "/" + m.file)
                let (tmp, r) = try await URLSession.shared.download(from: url)
                guard (r as? HTTPURLResponse)?.statusCode == 200 else { throw UpdateError.msg("Update archive is not available") }
                let zip = work.appendingPathComponent("update.zip")
                try fm.moveItem(at: tmp, to: zip)
                let data = try Data(contentsOf: zip, options: .mappedIfSafe)
                guard data.count == m.size,
                      SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() == m.sha256
                else { throw UpdateError.msg("Downloaded file is damaged (checksum mismatch)") }
                phase = .installing
                let unz = work.appendingPathComponent("new")
                try run("/usr/bin/ditto", ["-x", "-k", zip.path, unz.path])
                let newApp = unz.appendingPathComponent("Reaper to Pro Tools.app")
                guard let info = NSDictionary(contentsOf: newApp.appendingPathComponent("Contents/Info.plist")),
                      info["CFBundleIdentifier"] as? String == Bundle.main.bundleIdentifier,
                      info["CFBundleShortVersionString"] as? String == m.version
                else { throw UpdateError.msg("Archive content does not match the manifest") }
                var dest = Bundle.main.bundleURL
                if Updater.isTranslocated {                           // запуск из Загрузок: macOS даёт копию только для чтения — ставим в «Программы»
                    let name = dest.lastPathComponent
                    let apps = URL(fileURLWithPath: "/Applications")
                    let home = fm.homeDirectoryForCurrentUser.appendingPathComponent("Applications")
                    if fm.isWritableFile(atPath: apps.path) {
                        dest = apps.appendingPathComponent(name)
                    } else {
                        try? fm.createDirectory(at: home, withIntermediateDirectories: true)
                        dest = home.appendingPathComponent(name)
                    }
                }
                guard fm.isWritableFile(atPath: dest.deletingLastPathComponent().path) else {
                    throw UpdateError.msg("No permission to replace the app in \(dest.deletingLastPathComponent().path). Move the app to a folder you can write to (e.g. Applications) and retry.")
                }
                let script = work.appendingPathComponent("swap.sh")
                try """
                #!/bin/sh
                while kill -0 \(ProcessInfo.processInfo.processIdentifier) 2>/dev/null; do sleep 0.3; done
                DEST="\(dest.path)"
                NEW="\(newApp.path)"
                BAK="\(work.path)/old.app"
                if [ -e "$DEST" ]; then mv "$DEST" "$BAK" || exit 1; fi
                if ditto "$NEW" "$DEST"; then
                    xattr -cr "$DEST"
                    codesign --force --deep --sign - "$DEST" >/dev/null 2>&1
                    open "$DEST"
                    rm -rf "$(dirname "$0")"
                else
                    rm -rf "$DEST"; [ -e "$BAK" ] && mv "$BAK" "$DEST"; open "$DEST"
                fi
                """.write(to: script, atomically: true, encoding: .utf8)
                let p = Process()
                p.executableURL = URL(fileURLWithPath: "/bin/sh")
                p.arguments = [script.path]
                p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
                try p.run()
                NSApp.terminate(nil)
            } catch {
                phase = .failed(error.localizedDescription); showResult = true
            }
        }
    }

    private func run(_ exe: String, _ args: [String]) throws {
        let p = Process(); p.executableURL = URL(fileURLWithPath: exe); p.arguments = args
        try p.run(); p.waitUntilExit()
        if p.terminationStatus != 0 { throw UpdateError.msg("\(exe) failed (\(p.terminationStatus))") }
    }
}

struct UpdateUI: ViewModifier {
    @ObservedObject var u: Updater
    func body(content: Content) -> some View {
        content
            .task { if ProcessInfo.processInfo.environment["RTP_NO_UPDATE_CHECK"] == nil { u.check(manual: false) } }
            .alert("Update available", isPresented: $u.askInstall) {
                Button("Install and restart") { u.install() }
                Button("Later", role: .cancel) {}
            } message: {
                Text("Version \(u.manifest?.version ?? "") is available (you have \(appVersion)).\n\n\(u.manifest?.notes ?? "")"
                     + (Updater.isTranslocated ? "\n\nThe app is running from a protected temporary location, so the new version will be installed into Applications." : ""))
            }
            .alert(resultTitle, isPresented: $u.showResult) { Button("OK", role: .cancel) {} } message: { Text(resultText) }
            .overlay(alignment: .bottom) {
                if u.phase == .downloading || u.phase == .installing {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text(u.phase == .downloading ? "Downloading update…" : "Installing update…").font(.system(size: 11))
                    }
                    .padding(8).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8)).padding(10)
                }
            }
    }
    private var resultTitle: String {
        if case .failed = u.phase { return "Update failed" }
        return "No updates"
    }
    private var resultText: String {
        if case .failed(let s) = u.phase { return s }
        return "You have the latest version (\(appVersion))."
    }
}
