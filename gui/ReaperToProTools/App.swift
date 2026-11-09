// Reaper → Pro Tools: окно над reaper_to_ptx.py (пишет .ptx напрямую).
// Input (проект Reaper) → Preflight (недостающие файлы) → Output → Convert.

import SwiftUI
import AppKit
import UniformTypeIdentifiers

// MARK: - Сводка проекта (вывод `reaper_to_ptx.py --info`)

struct MissingFile: Decodable, Identifiable, Hashable {
    let path: String          // как записано в проекте Reaper
    let name: String          // имя без пути
    let seconds: Double       // сколько секунд файла нужно проекту
    var id: String { path }
}

struct Candidate: Decodable, Identifiable, Hashable {
    let path: String
    let exact: Bool
    let frames: Int
    let rate: Int
    let bits: Int
    let channels: Int
    let seconds: Double
    let format_ok: Bool
    let duration_ok: Bool
    var id: String { path }
}

struct CandidateResult: Decodable {
    let missing: [MissingFile]
    let candidates: [String: [Candidate]]
}

/// Дорожка из `--info` (track_list): для выбора, что переносить.
struct TrackRow: Decodable, Identifiable, Hashable {
    let index: Int
    let name: String
    let kind: String
    let depth: Int
    let clips: Int
    let color: Int?                // 0xRRGGBB или nil
    let hidden: Bool?
    let inactive: Bool?
    let frozen: Bool?
    var id: Int { index }
}

struct ProjectInfo: Decodable {
    struct Video: Decodable { let tracks: Int; let clips: Int; let files: Int; let missing: Int? }

    let reaper_version: String
    let start_tc: String?
    let pan_law_db: Double?
    let sample_rate: Int
    let fps: Double
    let bpm: Double
    let time_signature: String
    let tracks: Int
    let clips: Int
    let fades: Int
    let files: Int
    let markers: Int
    let importable_clips: Int
    let folders: Int?
    let split_clips: Int?
    let pt_tracks: Int?
    let sessions: Int?
    let missing_files: [String]
    let missing_detail: [MissingFile]
    let video: Video
    let track_list: [TrackRow]?
}

enum Phase { case idle, converting, done, failed }

// Накопитель строк для вывода процесса (прогресс приходит через \r).
final class LineBuffer: @unchecked Sendable {
    private var pending = ""
    private let lock = NSLock()

    func feed(_ text: String) -> [String] {
        lock.lock(); defer { lock.unlock() }
        pending += text
        var lines = pending.components(separatedBy: CharacterSet(charactersIn: "\r\n"))
        pending = lines.removeLast()
        return lines.filter { !$0.isEmpty }
    }
}

// MARK: - Модель

@MainActor
final class Model: ObservableObject {
    @Published var rppURL: URL?
    @Published var info: ProjectInfo?
    @Published var infoError: String?
    @Published var loading = false

    @Published var relinkMap: [String: String] = [:]      // подтверждённые связки: путь из проекта -> файл
    @Published var copyMedia = true
    @Published var transferFades = true
    @Published var xGain = true
    @Published var xFader = true
    @Published var xAuto = true
    @Published var xPlugins = true
    @Published var xSends = true
    @Published var xRouting = true
    @Published var xMarkers = true
    @Published var xStates = true
    @Published var xFolders = true
    @Published var xLanes = true
    @Published var xVideo = true
    @Published var xGroups = true
    @Published var customDir: URL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
    /// «Save in same folder as source»: результат кладётся рядом с исходным файлом.
    @Published var sameFolder: Bool = UserDefaults.standard.object(forKey: "sameFolderAsSource") as? Bool ?? true {
        didSet { UserDefaults.standard.set(sameFolder, forKey: "sameFolderAsSource") }
    }
    var outputDir: URL {
        if sameFolder, let u = rppURL { return sameFolderOutput(for: u) }
        return customDir
    }
    @Published var projectName = ""
    /// Выбранные дорожки (индексы проекта); nil — все.
    @Published var selectedTracks: Set<Int>?

    @Published var phase: Phase = .idle
    @Published var progress: Double = 0
    @Published var status = ""
    @Published var log = ""
    @Published var resultURL: URL?

    private var process: Process?

    /// Движок: Python-скрипт из Resources/ptx_tools (пишет .ptx напрямую, Pro Tools не нужен).
    var script: String {
        if let r = Bundle.main.resourcePath {
            let p = r + "/ptx_tools/reaper_to_ptx.py"
            if FileManager.default.fileExists(atPath: p) { return p }
        }
        return FileManager.default.currentDirectoryPath + "/ptx_tools/reaper_to_ptx.py"
    }
    let python = "/usr/bin/python3"

    /// Связки Relink передаём движку JSON-файлом.
    func relinkArgs() -> [String] {
        guard !relinkMap.isEmpty,
              let data = try? JSONSerialization.data(withJSONObject: relinkMap) else { return [] }
        let f = NSTemporaryDirectory() + "rtp_relink_\(UUID().uuidString).json"
        FileManager.default.createFile(atPath: f, contents: data)
        return ["--relink", f]
    }

    func load(_ url: URL) {
        rppURL = url
        relinkMap = [:]
        selectedTracks = nil
        projectName = url.deletingPathExtension().lastPathComponent
        phase = .idle
        status = ""
        refreshInfo()
    }

    func refreshInfo() {
        guard let url = rppURL else { return }
        loading = true
        infoError = nil

        var args = [script, url.path, "--info"]
        args += relinkArgs()
        let exe = python

        Task.detached {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: exe)
            p.arguments = args
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err

            var result: ProjectInfo?
            var message: String?

            do {
                try p.run()
                let data = out.fileHandleForReading.readDataToEndOfFile()
                let errData = err.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()

                if let i = try? JSONDecoder().decode(ProjectInfo.self, from: data) {
                    result = i
                } else {
                    message = String(data: errData, encoding: .utf8) ?? "Cannot read the project."
                }
            } catch {
                message = "Cannot start Python: \(error.localizedDescription)"
            }

            await MainActor.run {
                self.info = result
                self.infoError = message
                self.loading = false
            }
        }
    }

    var canConvert: Bool {
        info != nil && phase != .converting && !projectName.trimmingCharacters(in: .whitespaces).isEmpty && selectedTracks?.isEmpty != true
    }

    func convert() {
        guard let url = rppURL, canConvert else { return }

        let outDir = outputDir
        guard let name = resolveExistingOutput(name: projectName.trimmingCharacters(in: .whitespaces),
                                               target: { outDir.appendingPathComponent($0) }) else { return }
        projectName = name
        var args = [script, url.path, "--out", outputDir.path, "--name", name]
        if !copyMedia { args.append("--link") }
        if !transferFades { args.append("--no-fades") }
        if !xGain { args.append("--no-gain") }
        if !xFader { args.append("--no-fader") }
        if !xAuto { args.append("--no-automation") }
        if !xPlugins { args.append("--no-plugins") }
        if !xSends { args.append("--no-sends") }
        if !xRouting { args.append("--no-routing") }
        if !xMarkers { args.append("--no-markers") }
        if !xStates { args.append("--no-states") }
        if !xFolders { args.append("--no-folders") }
        if !xLanes { args.append("--no-lanes") }
        if !xVideo { args.append("--no-video") }
        if !xGroups { args.append("--no-groups") }
        if let t = selectedTracks { args += ["--tracks", t.sorted().map(String.init).joined(separator: ",")] }
        args += relinkArgs()

        phase = .converting
        progress = 0
        status = "Starting…"
        log = ""
        resultURL = nil

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        process = p

        // Вывод приходит кусками; прогресс печатается через \r.
        let buffer = LineBuffer()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            let ready = buffer.feed(text)
            Task { @MainActor in ready.forEach { self?.consume($0) } }
        }

        p.terminationHandler = { [weak self] proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in self?.finish(code: proc.terminationStatus, folder: self?.outputDir.appendingPathComponent(name)) }
        }

        do {
            try p.run()
        } catch {
            phase = .failed
            status = "Cannot start Python: \(error.localizedDescription)"
        }
    }

    private func consume(_ line: String) {
        // "Placing clips: 120/501"
        if let r = line.range(of: #"^(.+): (\d+)/(\d+)$"#, options: .regularExpression) {
            let parts = line[r].components(separatedBy: ": ")
            let nums = parts.last!.split(separator: "/").compactMap { Double($0) }
            if parts.count >= 2, nums.count == 2, nums[1] > 0 {
                progress = nums[0] / nums[1]
                status = nums[1] <= 1 ? "\(parts[0])…" : "\(parts[0]): \(Int(nums[0])) of \(Int(nums[1]))"
            }
            return
        }

        log += line + "\n"

        if line.hasPrefix("Done:") {
            status = "Done! " + line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        }
    }

    private func finish(code: Int32, folder: URL?) {
        process = nil

        if code == 0 || code == 2 {
            phase = .done
            progress = 1
            resultURL = folder
            if !status.hasPrefix("Done!") { status = "Done!" }
            if code == 2 { status += " (some clips or fades failed — see log)" }
            if !copyMedia && log.contains("copying instead of linking") { status += " Warning: files were copied, not linked — the disk of the sources is unknown (see log)." }
        } else {
            phase = .failed
            let lastError = log.split(separator: "\n").last.map(String.init) ?? "unknown error"
            status = "Failed: \(lastError)"
        }
    }

    func cancel() {
        process?.terminate()
    }
}

// MARK: - Вьюхи

struct Card<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .semibold)).padding(.leading, 4)
            VStack(alignment: .leading, spacing: 8) { content }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.primary.opacity(0.08)))
        }
    }
}

/// Строка-галочка во всплывающем окне опций.
struct OptionRow: View {
    let title: String
    @Binding var isOn: Bool
    var body: some View {
        Toggle(title, isOn: $isOn).toggleStyle(.checkbox).font(.system(size: 13))
    }
}

/// Кнопка «What to transfer…» + popover со списком опций; рядом — краткая сводка.
struct TransferButton<Content: View>: View {
    let allOn: Bool
    @ViewBuilder var content: Content
    @State private var shown = ProcessInfo.processInfo.environment["RTP_POPOVER"] != nil
    var body: some View {
        HStack {
            Button("What to transfer…") { shown.toggle() }
                .popover(isPresented: $shown, arrowEdge: .bottom) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("What to transfer").font(.system(size: 15, weight: .bold))
                        content
                    }
                    .padding(16)
                    .frame(minWidth: 280, alignment: .leading)
                }
            Spacer()
        }
    }
}

/// Карточка «Tracks» (после Preflight): что переносить. Фильтр по имени и по состоянию / типу (как в расширении Reaper),
/// папки сворачиваются, галочка папки включает и выключает всё её содержимое; All / None — для показанных. selected == nil — все дорожки.
struct TracksCard: View {
    static let width: CGFloat = 340
    let tracks: [TrackRow]
    @Binding var selected: Set<Int>?
    @State private var expanded: Set<Int> = []          // папки по умолчанию свёрнуты: храним раскрытые
    @State private var query = ""
    @State private var hiddenGroups: Set<String> = []

    var sel: Set<Int> { selected ?? Set(tracks.map(\.index)) }
    func setSel(_ s: Set<Int>) { selected = s.count == tracks.count ? nil : s }

    static func kindGroup(_ k: String) -> String {
        switch k.lowercased() {
        case "folder", "routing": return "folder"
        case "audio", "aux", "vca", "video": return k.lowercased()
        default: return "other"
        }
    }
    static let groupTitles: [(String, String)] = [("inactive", "Inactive Tracks"), ("hidden", "Hidden Tracks"), ("frozen", "Frozen Tracks"), ("noclips", "Tracks without Clips"),
                                                   ("folder", "Folder Tracks"), ("audio", "Audio Tracks"), ("aux", "Aux Inputs"), ("vca", "VCA Masters"), ("video", "Video Tracks"), ("other", "Other Tracks")]

    func groups(_ t: TrackRow) -> [String] {
        var g = [Self.kindGroup(t.kind)]
        if t.inactive == true { g.append("inactive") }
        if t.hidden == true { g.append("hidden") }
        if t.frozen == true { g.append("frozen") }
        if t.clips == 0 && g[0] != "folder" { g.append("noclips") }
        return g
    }
    /// Пункты меню фильтра — только те, что встречаются в этой сессии.
    var presentGroups: [(String, String)] { let have = Set(tracks.flatMap(groups)); return Self.groupTitles.filter { have.contains($0.0) } }
    var filterActive: Bool { !query.isEmpty || !hiddenGroups.isEmpty }

    /// Содержимое папки: следующие дорожки глубже неё.
    func descendants(_ i: Int) -> [Int] {
        guard let p = tracks.firstIndex(where: { $0.index == i }) else { return [] }
        return tracks[(p + 1)...].prefix { $0.depth > tracks[p].depth }.map(\.index)
    }
    var visible: [TrackRow] {
        var out: [TrackRow] = [], hideBelow: Int? = nil
        for t in tracks {
            if let d = hideBelow { if t.depth > d { continue } else { hideBelow = nil } }
            if query.isEmpty && Self.kindGroup(t.kind) == "folder" && !expanded.contains(t.index) { hideBelow = t.depth }      // при поиске свёрнутые папки не прячут найденное
            let okName = query.isEmpty || t.name.localizedCaseInsensitiveContains(query)
            if okName && hiddenGroups.isDisjoint(with: groups(t)) { out.append(t) }
        }
        return out
    }

    var body: some View {
        Card(title: "Tracks") {
            if tracks.isEmpty {
                Detail(text: "—")
                Spacer(minLength: 0)
            } else {
                let shown = visible
                HStack(spacing: 8) {
                    TextField("Filter", text: $query).textFieldStyle(.roundedBorder).controlSize(.small)
                    Menu {
                        ForEach(presentGroups, id: \.0) { g in
                            Toggle(g.1, isOn: Binding(get: { !hiddenGroups.contains(g.0) },
                                                      set: { if $0 { hiddenGroups.remove(g.0) } else { hiddenGroups.insert(g.0) } }))
                        }
                        Divider()
                        Button("Show All") { hiddenGroups = []; query = "" }
                    } label: {
                        Image(systemName: filterActive ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle")
                    }
                    .menuStyle(.borderlessButton).fixedSize().help("Show tracks by state and type")
                    if tracks.contains(where: { Self.kindGroup($0.kind) == "folder" }) {
                        Button(expanded.isEmpty ? "Expand All" : "Collapse All") {
                            expanded = expanded.isEmpty ? Set(tracks.filter { Self.kindGroup($0.kind) == "folder" }.map(\.index)) : []
                        }
                        .controlSize(.small)
                    }
                }
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(shown) { t in row(t); Divider() }
                    }
                }
                .frame(minHeight: 120, idealHeight: 120, maxHeight: .infinity)   // маленькая «идеальная» высота: окно не растёт от длинного списка
                .overlay { if shown.isEmpty { Detail(text: "No tracks match the filter.") } }
                HStack {
                    Button("All") { setSel(sel.union(shown.map(\.index))) }.controlSize(.small)
                    Button("None") { setSel(sel.subtracting(shown.map(\.index))) }.controlSize(.small)
                    Spacer()
                    Text("\(sel.count) of \(tracks.count)" + (shown.count != tracks.count ? " · \(shown.count) shown" : ""))
                        .font(.system(size: 11)).foregroundStyle(sel.isEmpty ? Color.red : Color.secondary)
                }
            }
        }
        .frame(width: Self.width)
        .frame(maxHeight: .infinity, alignment: .top)
    }

    func row(_ t: TrackRow) -> some View {
        let folder = Self.kindGroup(t.kind) == "folder"
        let kids = folder ? descendants(t.index) : []
        let dim = t.inactive == true || t.hidden == true
        return HStack(spacing: 6) {
            Color.clear.frame(width: CGFloat(t.depth) * 16, height: 1)
            if folder && !kids.isEmpty {
                Button { if expanded.contains(t.index) { expanded.remove(t.index) } else { expanded.insert(t.index) } } label: {
                    Image(systemName: "chevron.right").rotationEffect(.degrees(expanded.contains(t.index) ? 90 : 0))
                        .font(.system(size: 11, weight: .semibold)).frame(width: 12)
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
            } else {
                Color.clear.frame(width: 12, height: 1)
            }
            Toggle("", isOn: Binding(get: { sel.contains(t.index) }, set: { on in
                var s = sel
                for i in [t.index] + kids { if on { s.insert(i) } else { s.remove(i) } }
                setSel(s)
            })).toggleStyle(.checkbox).labelsHidden()
            RoundedRectangle(cornerRadius: 3)
                .fill(t.color.map { Color(red: Double($0 >> 16 & 255) / 255, green: Double($0 >> 8 & 255) / 255, blue: Double($0 & 255) / 255) } ?? Color.clear)
                .overlay(RoundedRectangle(cornerRadius: 3).stroke(Color.primary.opacity(0.15)))
                .frame(width: 13, height: 13)
            Text(t.name).font(.system(size: 14)).lineLimit(1).truncationMode(.middle)
                .foregroundStyle(dim ? Color.secondary : Color.primary)
            Spacer()
            Text(detail(t, folder: folder, kids: kids.count)).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding(.vertical, 5).padding(.horizontal, 4)
        .contentShape(Rectangle())
    }

    func detail(_ t: TrackRow, folder: Bool, kids: Int) -> String {
        var parts = [folder ? "folder · \(kids)" : (t.clips > 0 ? "\(t.kind) · \(t.clips) clip\(t.clips == 1 ? "" : "s")" : t.kind)]
        if t.inactive == true { parts.append("inactive") }
        if t.hidden == true { parts.append("hidden") }
        if t.frozen == true { parts.append("frozen") }
        return parts.joined(separator: " · ")
    }
}

/// Если результат уже существует — спрашивает: заменить (старый уходит в Корзину), создать с другим именем или отмена.
/// Возвращает итоговое имя или nil при отмене.
@MainActor
func resolveExistingOutput(name: String, target: (String) -> URL) -> String? {
    let fm = FileManager.default
    guard fm.fileExists(atPath: target(name).path) else { return name }
    let a = NSAlert()
    a.messageText = "“\(target(name).lastPathComponent)” already exists"
    a.informativeText = "Replace it (the old one goes to the Trash), or create the new one under another name?"
    a.addButton(withTitle: "Create with another name")
    a.addButton(withTitle: "Replace")
    a.addButton(withTitle: "Cancel")
    switch a.runModal() {
    case .alertFirstButtonReturn:
        var n = 2
        while fm.fileExists(atPath: target("\(name) \(n)").path) { n += 1 }
        return "\(name) \(n)"
    case .alertSecondButtonReturn:
        do { try fm.trashItem(at: target(name), resultingItemURL: nil); return name }
        catch {
            let e = NSAlert(); e.messageText = "Cannot move the old one to the Trash"; e.informativeText = error.localizedDescription; e.runModal()
            return nil
        }
    default:
        return nil
    }
}

let appVersion = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "1.1"

struct Detail: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 11)).foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct ContentView: View {
    @ObservedObject var m: Model
    let openAny: () -> Void
    @State private var showLog = false
    @State private var showPTLib = false
    @State private var showRelink = false

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 14) {
                inputCard
                preflightCard
                outputCard
                footer
            }
            .frame(width: 424)
            TracksCard(tracks: m.info?.track_list ?? [], selected: $m.selectedTracks).id(m.rppURL)   // новый файл — фильтр и раскрытые папки с нуля
        }
        .fixedSize(horizontal: false, vertical: true)      // высоту задаёт левая колонка, список дорожек растягивается до неё
        .onAppear {
            if ProcessInfo.processInfo.environment["RTP_RELINK"] != nil {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { showRelink = true }
            }
        }
        .sheet(isPresented: $showLog) { LogSheet(text: m.log, close: { showLog = false }) }
        .sheet(isPresented: $showPTLib) { PTLibrarySheet(python: m.python, toolsDir: (m.script as NSString).deletingLastPathComponent, close: { showPTLib = false }) }
        .sheet(isPresented: $showRelink) {
            RelinkSheet(model: RelinkModel(app: m), close: { showRelink = false })
        }
    }

    // MARK: Input

    var inputCard: some View {
        Card(title: "Input") {
            HStack {
                Text("Session file:").font(.system(size: 12))
                Text(m.rppURL?.lastPathComponent ?? "—")
                    .font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("Browse…") { openAny() }
            }

            if m.loading {
                HStack(spacing: 6) { ProgressView().controlSize(.small); Detail(text: "Reading project…") }
            } else if let e = m.infoError {
                Text(e).font(.system(size: 11)).foregroundStyle(.red)
            } else if let i = m.info {
                Detail(text: "Reaper \"\(i.reaper_version)\"")
                Detail(text: "Session SR: \(i.sample_rate) Hz, \(fmt(i.fps)) fps" + (i.start_tc.map { ", start \($0)" } ?? ""))
                Detail(text: "\(String(format: "%.2f", i.bpm)) BPM · \(i.time_signature)" + (i.pan_law_db.map { " · Pan law \(String(format: "%.1f", $0)) dB" } ?? ""))
                Detail(text: "\(i.tracks) tracks, \(i.clips) clips, \(i.fades) fades, \(i.files) files, \(i.markers) markers")
                if (i.folders ?? 0) > 0 {
                    Detail(text: "\(i.folders ?? 0) folders → Pro Tools folders (Reaper folder bus = Routing Folder; note \"Basic Folder\" = Basic)")
                }
                if (i.sessions ?? 1) > 1 {
                    Detail(text: "The project needs \(i.pt_tracks ?? 0) Pro Tools tracks; a session holds at most 512, so it will be split into \(i.sessions ?? 1) sessions (a folder with the sessions is created)")
                }
                if (i.split_clips ?? 0) > 0 {
                    Detail(text: "\(i.split_clips ?? 0) clips with a different channel layout (mono / stereo / 5.1) or with their own effects will go to extra tracks “name (stereo)” / “name (FX)” below their track; clip effects become inserts")
                }
                if i.video.clips > 0 {
                    Detail(text: "Video: \(i.video.tracks) track, \(i.video.clips) clip" + ((i.video.clips > 1) ? " (Pro Tools takes one video clip — the first is transferred)" : "") + ((i.video.missing ?? 0) > 0 ? " — \(i.video.missing ?? 0) not found on disk" : ""))
                }
            } else {
                Detail(text: "Choose or drop a Reaper project (.rpp)")
            }
        }
    }

    // MARK: Preflight

    var preflightCard: some View {
        Card(title: "Preflight") {
            if let i = m.info {
                if !i.missing_files.isEmpty {
                    HStack {
                        Label("\(i.missing_files.count) missing of \(i.files) files", systemImage: "exclamationmark.triangle.fill")
                            .font(.system(size: 12)).foregroundStyle(.orange)
                        Spacer()
                        Button("Relink…") { showRelink = true }
                    }
                }
                Label("Ready to convert", systemImage: "checkmark.circle.fill")
                    .font(.system(size: 12)).foregroundStyle(.green)
                if !i.missing_files.isEmpty {
                    Detail(text: "Clips with missing files stay in the session as offline regions — Pro Tools asks where the files are when you open it.")
                }
            } else {
                Detail(text: "—")
            }
        }
    }

    // MARK: Output

    var outputCard: some View {
        Card(title: "Output") {
            Picker("", selection: $m.copyMedia) {
                Text("Link to source").tag(false)
                Text("Copy Media (Standalone)").tag(true)
            }
            .pickerStyle(.segmented).labelsHidden()

            Detail(text: m.copyMedia
                   ? "Writes the .ptx directly — Pro Tools is not needed. Audio is copied into the session's Audio Files."
                   : "Writes the .ptx directly. Audio files stay where they are and the session references the originals (Pro Tools \"Add Audio\"); files on another disk are copied.")
            Button("Plug-in library…") { showPTLib = true }.buttonStyle(.link).font(.system(size: 11))
            TransferButton(allOn: m.transferFades && m.xGain && m.xFader && m.xAuto && m.xSends && m.xPlugins && m.xMarkers && m.xStates && m.xFolders && m.xLanes && m.xRouting && m.xVideo && m.xGroups) {
                OptionRow(title: "Fades and crossfades", isOn: $m.transferFades)
                OptionRow(title: "Clip gain (item volume)", isOn: $m.xGain)
                Divider()
                OptionRow(title: "Markers", isOn: $m.xMarkers)
                Divider()
                OptionRow(title: "Track mute / solo / color", isOn: $m.xStates)
                OptionRow(title: "Track volume / pan", isOn: $m.xFader)
                OptionRow(title: "Volume automation", isOn: $m.xAuto)
                OptionRow(title: "Sends (as aux tracks)", isOn: $m.xSends)
                OptionRow(title: "Plug-ins (known ones, with settings)", isOn: $m.xPlugins)
                OptionRow(title: "Item groups and named empty items → Clip Groups", isOn: $m.xGroups)
                Divider()
                OptionRow(title: "Folders", isOn: $m.xFolders)
                OptionRow(title: "Routing (folder buses, first output)", isOn: $m.xRouting)
                OptionRow(title: "Takes & Lanes → alternate playlists", isOn: $m.xLanes)
                Divider()
                OptionRow(title: "Video track (first video item)", isOn: $m.xVideo)
            }


            Toggle("Save in same folder as source", isOn: $m.sameFolder).toggleStyle(.checkbox).font(.system(size: 12))
            HStack {
                Text("Output folder:").font(.system(size: 12))
                Text(m.outputDir.lastPathComponent).font(.system(size: 12, weight: .medium))
                Spacer()
                Button("Browse…") { pickOutput() }.disabled(m.sameFolder)
            }

            HStack {
                Text("Project folder:").font(.system(size: 12))
                TextField("", text: $m.projectName).textFieldStyle(.roundedBorder)
            }
        }
    }

    // MARK: Footer

    var footer: some View {
        VStack(spacing: 8) {
            if m.phase == .converting {
                ProgressView(value: m.progress).progressViewStyle(.linear)
            }

            HStack(spacing: 10) {
                Button { showLog = true } label: { Image(systemName: "list.bullet") }
                    .help("Show log")

                Button {
                    switch m.phase {
                    case .done:
                        if let u = m.resultURL { NSWorkspace.shared.activateFileViewerSelecting([u]) }
                    case .converting:
                        m.cancel()
                    default:
                        m.convert()
                    }
                } label: {
                    Text(m.phase == .done ? "Reveal in Finder" : m.phase == .converting ? "Cancel" : "Convert to Pro Tools")
                        .frame(maxWidth: .infinity).padding(.vertical, 4)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(m.phase != .done && m.phase != .converting && !m.canConvert)
            }

            if !m.status.isEmpty {
                Text(m.status).font(.system(size: 11)).foregroundStyle(m.phase == .failed ? .red : .secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).lineLimit(2)
            }

            if m.phase == .done {
                Button("Convert again") { m.phase = .idle; m.status = "" }.buttonStyle(.link).font(.system(size: 11))
            }
        }
    }

    // MARK: Helpers

    func fmt(_ d: Double) -> String { d == d.rounded() ? String(Int(d)) : String(d) }

    func pickOutput() {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        p.canCreateDirectories = true
        if p.runModal() == .OK, let u = p.url { m.customDir = u }
    }
}

enum MatchMode: Hashable { case exact, loose }

@MainActor
final class RelinkModel: ObservableObject {
    let python: String
    let candidateArgs: ([URL]) -> [String]          // аргументы движка для поиска кандидатов (папки добавляются сюда)
    let onCommit: ([String: String]) -> Void
    let formatHint: String
    let durationHint: String
    @Published var missing: [MissingFile]
    @Published var folders: [URL] = []
    @Published var candidates: [String: [Candidate]] = [:]
    @Published var chosen: [String: String] = [:]          // путь из проекта -> выбранный файл
    @Published var selected: String?
    @Published var mode: MatchMode = .exact
    @Published var matchFormat = true
    @Published var matchDuration = true
    @Published var searching = false
    @Published var searched = false
    @Published var error: String?

    init(missing: [MissingFile], python: String, formatHint: String, durationHint: String,
         candidateArgs: @escaping ([URL]) -> [String], onCommit: @escaping ([String: String]) -> Void) {
        self.python = python
        self.missing = missing
        self.formatHint = formatHint
        self.durationHint = durationHint
        self.candidateArgs = candidateArgs
        self.onCommit = onCommit
        self.selected = missing.first?.path
    }

    /// Relink для конвертации Reaper → Pro Tools.
    convenience init(app: Model) {
        self.init(missing: app.info?.missing_detail ?? [], python: app.python,
                  formatHint: "Match Format (mono, 24-bit, 48 kHz)", durationHint: "Match Duration (long enough for the project)",
                  candidateArgs: { [script = app.script, rpp = app.rppURL, relink = app.relinkArgs()] folders in
                      var a = [script, rpp?.path ?? "", "--candidates"]
                      for f in folders { a += ["--search", f.path] }
                      return a + relink },
                  onCommit: { [weak app] map in
                      guard let app else { return }
                      for (k, v) in map { app.relinkMap[k] = v }
                      app.refreshInfo() })
    }

    /// Кандидаты выбранного файла с учётом параметров сопоставления.
    func visible(_ path: String) -> [Candidate] {
        (candidates[path] ?? []).filter {
            (mode == .loose || $0.exact) && (!matchFormat || $0.format_ok) && (!matchDuration || $0.duration_ok)
        }
    }

    func addFolders() {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        p.allowsMultipleSelection = true
        p.message = "Choose folders to search for the missing files (searched recursively)"
        if p.runModal() == .OK {
            for u in p.urls where !folders.contains(u) { folders.append(u) }
        }
    }

    func findCandidates() {
        searching = true
        error = nil
        let args = candidateArgs(folders)
        let python = self.python

        Task.detached {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = args
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            var result: CandidateResult?
            var message: String?
            do {
                try p.run()
                let data = out.fileHandleForReading.readDataToEndOfFile()
                let errData = err.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                if let r = try? JSONDecoder().decode(CandidateResult.self, from: data) {
                    result = r
                } else {
                    message = String(data: errData, encoding: .utf8) ?? "Search failed."
                }
            } catch {
                message = "Cannot start Python: \(error.localizedDescription)"
            }
            await MainActor.run {
                self.searching = false
                self.searched = true
                if let r = result {
                    self.candidates = r.candidates
                    // если подходит ровно один файл — привязываем сразу
                    for m in self.missing where self.chosen[m.path] == nil {
                        let v = self.visible(m.path)
                        if v.count == 1 { self.chosen[m.path] = v[0].path }
                    }
                } else {
                    self.error = message
                }
            }
        }
    }

    func commit() { onCommit(chosen) }
}

struct RelinkSheet: View {
    @StateObject var model: RelinkModel
    let close: () -> Void

    init(model: RelinkModel, close: @escaping () -> Void) {
        _model = StateObject(wrappedValue: model)
        self.close = close
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Relink").font(.system(size: 22, weight: .bold))
                Spacer()
                Text("\(model.missing.count) missing · \(model.chosen.count) linked")
                    .font(.system(size: 13)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 18).padding(.vertical, 14)
            Divider()

            HStack(alignment: .top, spacing: 0) {
                filesColumn.frame(width: 330)
                Divider()
                optionsColumn
            }

            Divider()
            HStack {
                Spacer()
                Button("Cancel", action: close).keyboardShortcut(.cancelAction)
                Button("Commit Links") { model.commit(); close() }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.chosen.isEmpty)
            }
            .padding(14)
        }
        .frame(width: 880, height: 600)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    // MARK: слева — недостающие файлы

    var filesColumn: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Files to Relink").font(.system(size: 15, weight: .semibold))
            if model.missing.isEmpty {
                Text("Nothing is missing.").foregroundStyle(.secondary).font(.system(size: 12))
            }
            ScrollView {
                VStack(spacing: 0) {
                    ForEach(model.missing) { f in
                        HStack(spacing: 8) {
                            Image(systemName: model.chosen[f.path] != nil ? "checkmark.circle.fill" : "circle.dashed")
                                .foregroundStyle(model.chosen[f.path] != nil ? Color.green : Color.secondary)
                            Text(f.name).font(.system(size: 13)).lineLimit(1).truncationMode(.middle)
                            Spacer()
                        }
                        .padding(.vertical, 7).padding(.horizontal, 8)
                        .background(model.selected == f.path ? Color.accentColor.opacity(0.25) : Color.clear)
                        .contentShape(Rectangle())
                        .onTapGesture { model.selected = f.path }
                        Divider()
                    }
                }
            }
        }
        .padding(16)
        .frame(maxHeight: .infinity, alignment: .topLeading)
    }

    // MARK: справа — параметры, папки, кандидаты

    var optionsColumn: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Linking Options").font(.system(size: 15, weight: .semibold))
            HStack(alignment: .top) {
                Text("Match by").font(.system(size: 13))
                Picker("", selection: $model.mode) {
                    Text("Find By Name").tag(MatchMode.exact)
                    Text("Find By Name (ignore case and symbols)").tag(MatchMode.loose)
                }
                .pickerStyle(.radioGroup).labelsHidden()
            }
            Toggle(model.formatHint, isOn: $model.matchFormat).toggleStyle(.checkbox)
            Toggle(model.durationHint, isOn: $model.matchDuration).toggleStyle(.checkbox)
            Divider()

            HStack {
                Text("Areas to Search").font(.system(size: 15, weight: .semibold))
                Spacer()
                Button("Add Folder…") { model.addFolders() }
                Button("Find Candidates") { model.findCandidates() }
                    .disabled(model.folders.isEmpty || model.searching || model.missing.isEmpty)
            }
            if model.folders.isEmpty {
                Text("No folder selected. Add a folder to search for missing files.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(model.folders, id: \.self) { u in
                        HStack(spacing: 6) {
                            Image(systemName: "folder").foregroundStyle(.secondary)
                            Text(u.path).font(.system(size: 12)).lineLimit(1).truncationMode(.head)
                            Spacer()
                            Button { model.folders.removeAll { $0 == u } } label: { Image(systemName: "xmark.circle") }
                                .buttonStyle(.plain).foregroundStyle(.secondary)
                        }
                    }
                }
                Text("Selected folders are searched recursively, including all subfolders.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if model.searching { HStack(spacing: 6) { ProgressView().controlSize(.small); Text("Searching…").font(.system(size: 12)) } }
            if let e = model.error { Text(e).font(.system(size: 11)).foregroundStyle(.red) }
            Divider()

            Text("Candidates").font(.system(size: 15, weight: .semibold))
            candidatesList
            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    @ViewBuilder
    var candidatesList: some View {
        if let sel = model.selected, let file = model.missing.first(where: { $0.path == sel }) {
            let list = model.visible(sel)
            if !model.searched {
                Text("Add a folder and press Find Candidates.").font(.system(size: 12)).foregroundStyle(.secondary)
            } else if list.isEmpty {
                Text("No candidates for \(file.name) with the current options.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(list) { c in
                            let on = model.chosen[sel] == c.path
                            HStack(spacing: 8) {
                                Image(systemName: on ? "largecircle.fill.circle" : "circle")
                                    .foregroundStyle(on ? Color.accentColor : Color.secondary)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(URL(fileURLWithPath: c.path).lastPathComponent).font(.system(size: 13))
                                    Text(URL(fileURLWithPath: c.path).deletingLastPathComponent().path)
                                        .font(.system(size: 11)).foregroundStyle(.secondary)
                                        .lineLimit(1).truncationMode(.head)
                                }
                                Spacer()
                                Text(String(format: "%.1f s · %d-bit · %d ch · %d Hz", c.seconds, c.bits, c.channels, c.rate))
                                    .font(.system(size: 11)).foregroundStyle(.secondary)
                            }
                            .padding(.vertical, 6).padding(.horizontal, 6)
                            .contentShape(Rectangle())
                            .onTapGesture { model.chosen[sel] = on ? nil : c.path }
                            Divider()
                        }
                    }
                }
                .frame(maxHeight: 190)
            }
        } else {
            Text("Select a missing file to see candidates.").font(.system(size: 12)).foregroundStyle(.secondary)
        }
    }
}

struct FXLibrarySheet: View {
    let python: String
    let toolsDir: String
    let close: () -> Void
    @State private var count = 0
    @State private var scriptPath = ""

    func run(_ args: [String]) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [toolsDir + "/reaper_fx.py"] + args
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        try? p.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Plug-in library for Reaper").font(.headline)
            Text("Pro Tools plug-ins can be re-created in Reaper only from an FX block that Reaper itself writes (it contains the plug-in's default state). Reaper can export these blocks for the plug-ins installed on this Mac:")
                .font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 4) {
                Text("1. Click “Prepare script” — the script and the list of wanted plug-ins are placed in the library folder.")
                Text("2. In Reaper: Actions ▸ Show action list ▸ New action ▸ Load ReaScript…, choose export_fx_library.lua and run it.")
                Text("3. Wait for “Finished” in the Reaper console (if Reaper quits on a plug-in, run the script again — it skips that plug-in).")
                Text("4. Convert the Pro Tools session again — the FX chains appear.")
            }
            .font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Prepare script") {
                    scriptPath = run(["--prepare"])
                    if !scriptPath.isEmpty { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: scriptPath)]) }
                }
                Button("Refresh") { count = Int(run(["--count"])) ?? 0 }
                Text("Library: \(count) plug-ins").font(.system(size: 12)).foregroundStyle(.secondary)
            }
            HStack { Spacer(); Button("Close", action: close) }
        }
        .padding(18)
        .frame(width: 560)
        .onAppear { count = Int(run(["--count"])) ?? 0 }
    }
}


struct PTLibrarySheet: View {
    let python: String
    let toolsDir: String
    let close: () -> Void
    @State private var count = 0
    @State private var message = ""

    func run(_ args: [String], script: String = "ptx_plugin_learn.py") -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [toolsDir + "/" + script] + args
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        try? p.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func scan() {
        message = "Scanning…"
        let out = run(["--scan"], script: "ptx_plugin_scan.py")
        if let data = out.data(using: .utf8), let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let found = (obj["found"] as? Int) ?? 0
            message = found == 0 ? "No new plug-ins found among installed AAX plug-ins (only those that ship a DigiPageTable.xml can be recognised)."
                                 : "Recognised \(found) installed plug-in(s); Reaper plug-ins with these names can now be created in Pro Tools."
        } else {
            message = "Could not scan the plug-ins."
        }
    }

    func learn() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.allowedFileTypes = ["ptx"]
        guard panel.runModal() == .OK else { return }
        message = "Reading…"
        let out = run(["--learn"] + panel.urls.map { $0.path })
        if let data = out.data(using: .utf8),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let added = obj["added"] as? [String] {
            count = (obj["total"] as? Int) ?? count
            message = added.isEmpty ? "No new plug-ins in the selected session(s)."
                                    : "Added \(added.count): " + added.prefix(12).joined(separator: ", ") + (added.count > 12 ? "…" : "")
        } else {
            message = "Could not read the session."
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Pro Tools plug-in library").font(.headline)
            Text("Reaper plug-ins are re-created in Pro Tools from slots copied out of real Pro Tools sessions (a plug-in's state cannot be generated). The app already knows many plug-ins. To add more, choose a Pro Tools session that has them inserted on tracks of the format you need (mono, stereo or 5.1) — new plug-ins are added to your personal library with the settings they have in that session.")
                .font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Learn plug-ins from a session…") { learn() }
                Button("Scan installed plug-ins") { scan() }
                Text("Personal library: \(count)").font(.system(size: 12)).foregroundStyle(.secondary)
            }
            if !message.isEmpty { Text(message).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            HStack { Spacer(); Button("Close", action: close) }
        }
        .padding(18)
        .frame(width: 560)
        .onAppear { count = Int(run(["--count"])) ?? 0 }
    }
}


struct LogSheet: View {
    let text: String
    let close: () -> Void

    var body: some View {
        VStack(alignment: .leading) {
            Text("Log").font(.headline)
            ScrollView {
                Text(text.isEmpty ? "(empty)" : text)
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(width: 520, height: 300)
            HStack { Spacer(); Button("Close", action: close) }
        }
        .padding(16)
    }
}

// Скрытая отладка: RTP_SNAPSHOT=/путь/out.png — сохранить PNG своего окна и выйти.
@MainActor
func scheduleSnapshot() {
    guard let path = ProcessInfo.processInfo.environment["RTP_SNAPSHOT"] else { return }

    DispatchQueue.main.asyncAfter(deadline: .now() + (ProcessInfo.processInfo.environment["RTP_TRACKS"] != nil ? 12 : ProcessInfo.processInfo.environment["RTP_RELINK"] != nil ? 6 : 3)) {
        let main = NSApp.windows.first(where: { $0.isVisible && !$0.isSheet })
        let sheet = main?.attachedSheet ?? NSApp.windows.first(where: { $0.isSheet && $0.isVisible })
        let pop = ProcessInfo.processInfo.environment["RTP_POPOVER"] != nil ? NSApp.windows.first(where: { $0.isVisible && String(describing: type(of: $0)).contains("Popover") }) : nil
        if let view = (pop ?? sheet ?? main)?.contentView,
           let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: rep)
            if let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: URL(fileURLWithPath: path))
            }
        }
        exit(0)                                   // terminate не срабатывает, пока открыт лист
    }
}

// MARK: - Pro Tools → Reaper (ptx_to_reaper.py)

struct PtxInfo: Decodable {
    let tracks: Int
    let clips: Int
    let markers: Int
    let marker_regions: Int?
    let track_markers: Int?
    let markers_dropped: Int?
    let plugin_states: Int?
    let rate: Int
    let missing: [String]
    let missing_detail: [MissingFile]?
    let files: Int?
    let skipped: Int
    let folders: Int?
    let routing_folders: Int?
    let alt_playlists: Int?
    let skipped_tracks: Int?
    struct SkippedTrack: Decodable { let name: String; let reason: String }
    let skipped_detail: [SkippedTrack]?
    let app_info: String?
    let start_tc: String?
    let fps: Double?
    let bits: Int?
    let audio_tracks: Int?
    let clips_raw: Int?
    let files_used: Int?
    let fades: Int?
    let fades_raw: Int?
    let vol_points: Int?
    let clip_gain_entries: Int?
    let video_tracks: Int?
    let video_clips: Int?
    let video_files: Int?
    let plugins: Int?
    let sends: Int?
    let track_list: [TrackRow]?

    /// «48000 Hz, 24-bit, 25 fps, start 00:58:30»
    var rateLine: String {
        var l = "\(rate) Hz"
        if let b = bits { l += ", \(b)-bit" }
        if let f = fps { l += ", \(f == f.rounded() ? String(Int(f)) : String(f)) fps" }
        if let t = start_tc { l += ", start \(t)" }
        return l
    }
}

/// «Save in same folder as source»: если исходный файл лежит в папке своей сессии (папка называется так же, как файл) — результат кладётся
/// в материнскую папку (рядом с папкой сессии), иначе — рядом с файлом.
func sameFolderOutput(for source: URL) -> URL {
    let dir = source.deletingLastPathComponent()
    let stem = source.deletingPathExtension().lastPathComponent
    if dir.lastPathComponent.caseInsensitiveCompare(stem) == .orderedSame { return dir.deletingLastPathComponent() }
    return dir
}

struct TrackDataItem { let key: String; let title: String; let on: Bool; let supported: Bool }

/// Пункты Track Data (как в списке Pro Tools): группы разделяются линиями; последняя группа — наши дополнения.
enum TrackDataGroups {
    static func i(_ k: String, _ t: String, _ on: Bool = true, _ sup: Bool = true) -> TrackDataItem { TrackDataItem(key: k, title: t, on: on, supported: sup) }
    static let all: [[TrackDataItem]] = [
        [i("clips", "Clips and Media"), i("clip_gain", "Clip Gain"), i("clip_mute", "Clip Mute"), i("fades", "Fade Settings")],
        [i("auto_vol", "Main Volume Automation"), i("auto_pan", "Main Pan Automation (up to 7.1.2)"), i("auto_mute", "Main Mute Automation"), i("main_routing", "Main Routing Assignments")],
        [i("send_auto_vol", "Send Volume Automation"), i("send_auto_pan", "Send Pan Automation"), i("send_auto_mute", "Send Mute Automation"), i("send_routing", "Send Routing Assignments")],
        [i("clip_colors", "Clip Colors"), i("track_colors", "Track Colors"), i("notes", "Track Notes"), i("solo", "Solo State"), i("solo_safe", "Solo Safe", false, false),
         i("playlists", "Playlists"), i("markers", "Memory Locations (Global Markers)"), i("tempo", "Tempo Map and Time Signature", false, false)],
        [i("skip_hidden", "Skip Hidden Tracks", false), i("skip_inactive", "Skip Inactive Tracks", false)],
        [i("trim_overlap", "Trim Overlapping Clips", false)],
        [i("mute", "Mute State"), i("fader", "Track Volume / Pan"), i("plugins", "Plug-ins (as FX chain)"), i("aux", "Aux / Instrument as empty tracks"), i("video", "Video track (video item)"), i("groups", "Clip groups (as grouped / empty items)"), i("vca", "VCA masters and their groups (as Reaper VCA)")],
    ]
}

@MainActor
final class ReverseModel: ObservableObject {
    @Published var ptxURL: URL?
    @Published var info: PtxInfo?
    @Published var infoError: String?
    @Published var loading = false
    @Published var searchDirs: [URL] = []
    @Published var relinkMap: [String: String] = [:]
    @Published var copyMedia = false
    @Published var altAsLanes = true
    /// Пункты Track Data: ключ -> включён.
    @Published var opt: [String: Bool] = Dictionary(uniqueKeysWithValues: TrackDataGroups.all.flatMap { $0 }.map { ($0.key, $0.on) })
    @Published var customDir: URL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
    /// «Save in same folder as source»: результат кладётся рядом с исходным файлом.
    @Published var sameFolder: Bool = UserDefaults.standard.object(forKey: "sameFolderAsSource") as? Bool ?? true {
        didSet { UserDefaults.standard.set(sameFolder, forKey: "sameFolderAsSource") }
    }
    var outputDir: URL {
        if sameFolder, let u = ptxURL { return sameFolderOutput(for: u) }
        return customDir
    }
    @Published var projectName = ""
    /// Выбранные дорожки (индексы сессии); nil — все.
    @Published var selectedTracks: Set<Int>?
    @Published var phase: Phase = .idle
    @Published var progress: Double = 0
    @Published var status = ""
    @Published var log = ""
    @Published var resultURL: URL?
    private var process: Process?

    var script: String {
        if let r = Bundle.main.resourcePath {
            let p = r + "/ptx_tools/ptx_to_reaper.py"
            if FileManager.default.fileExists(atPath: p) { return p }
        }
        return FileManager.default.currentDirectoryPath + "/ptx_tools/ptx_to_reaper.py"
    }
    let python = "/usr/bin/python3"

    var outputFile: URL {
        let n = projectName.trimmingCharacters(in: .whitespaces)
        return copyMedia ? outputDir.appendingPathComponent(n).appendingPathComponent(n + ".rpp")   // проект в своей папке + Media
                         : outputDir.appendingPathComponent(n + ".rpp")
    }
    var canConvert: Bool { info != nil && phase != .converting && !projectName.trimmingCharacters(in: .whitespaces).isEmpty && selectedTracks?.isEmpty != true }

    func dirArgs() -> [String] {
        var a = searchDirs.flatMap { ["--media-dir", $0.path] }
        if !relinkMap.isEmpty, let data = try? JSONSerialization.data(withJSONObject: relinkMap) {
            let f = NSTemporaryDirectory() + "rtp_relink_\(UUID().uuidString).json"
            FileManager.default.createFile(atPath: f, contents: data)
            a += ["--relink", f]
        }
        return a
    }

    func load(_ url: URL) {
        ptxURL = url
        relinkMap = [:]
        selectedTracks = nil
        projectName = url.deletingPathExtension().lastPathComponent
        phase = .idle
        status = ""
        refreshInfo()
    }

    func addSearchDir(_ u: URL) {
        if !searchDirs.contains(u) { searchDirs.append(u) }
        refreshInfo()
    }

    func refreshInfo() {
        guard let url = ptxURL else { return }
        loading = true
        infoError = nil
        let args = [script, url.path, "--info"] + dirArgs()
        let exe = python
        Task.detached {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: exe)
            p.arguments = args
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            var result: PtxInfo?
            var message: String?
            do {
                try p.run()
                let data = out.fileHandleForReading.readDataToEndOfFile()
                let errData = err.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                if let i = try? JSONDecoder().decode(PtxInfo.self, from: data) {
                    result = i
                } else {
                    message = String(data: errData, encoding: .utf8) ?? "Cannot read the session."
                }
            } catch {
                message = "Cannot start Python: \(error.localizedDescription)"
            }
            await MainActor.run {
                self.info = result
                self.infoError = message
                self.loading = false
            }
        }
    }

    /// Аргументы движка по выбранным пунктам Track Data.
    func transferArgs() -> [String] {
        func on(_ k: String) -> Bool { opt[k] ?? false }
        var a: [String] = []
        let flags = ["fades": "--no-fades", "clip_gain": "--no-gain", "markers": "--no-markers", "send_routing": "--no-sends",
                     "main_routing": "--no-folders", "fader": "--no-fader", "plugins": "--no-plugins", "aux": "--no-aux"]
        for (k, f) in flags.sorted(by: { $0.key < $1.key }) where !on(k) { a.append(f) }
        let offKeys = ["video", "groups", "vca", "clips", "clip_mute", "clip_colors", "track_colors", "notes", "solo", "mute", "auto_vol", "auto_pan", "auto_mute",
                       "send_auto_vol", "send_auto_pan", "send_auto_mute"]
        let off = offKeys.filter { !on($0) }
        if !off.isEmpty { a += ["--off", off.joined(separator: ",")] }
        let onKeys = ["skip_hidden", "skip_inactive", "trim_overlap"].filter { on($0) }
        if !onKeys.isEmpty { a += ["--on", onKeys.joined(separator: ",")] }
        if !on("playlists") { a.append("--no-alt-playlists") } else if !altAsLanes { a.append("--alt-as-tracks") }
        if let t = selectedTracks { a += ["--tracks", t.sorted().map(String.init).joined(separator: ",")] }
        return a
    }

    func convert() {
        guard let url = ptxURL, canConvert else { return }
        let outDir = outputDir, withMedia = copyMedia
        guard let newName = resolveExistingOutput(name: projectName.trimmingCharacters(in: .whitespaces), target: {
            withMedia ? outDir.appendingPathComponent($0) : outDir.appendingPathComponent($0 + ".rpp")
        }) else { return }
        projectName = newName
        let out = outputFile
        var args = [script, url.path, "--out", out.path] + dirArgs()
        if copyMedia { args += ["--copy-media"]; args[args.firstIndex(of: "--out")! + 1] = outputDir.appendingPathComponent(projectName.trimmingCharacters(in: .whitespaces) + ".rpp").path }
        args += transferArgs()
        phase = .converting
        progress = 0
        status = "Starting…"
        log = ""
        resultURL = nil
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        process = p
        let buffer = LineBuffer()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            let ready = buffer.feed(text)
            Task { @MainActor in ready.forEach { self?.consume($0) } }
        }
        p.terminationHandler = { [weak self] proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in self?.finish(code: proc.terminationStatus, file: out) }
        }
        do { try p.run() } catch {
            phase = .failed
            status = "Cannot start Python: \(error.localizedDescription)"
        }
    }

    private func consume(_ line: String) {
        if let r = line.range(of: #"^(.+): (\d+)/(\d+)$"#, options: .regularExpression) {
            let parts = line[r].components(separatedBy: ": ")
            let nums = parts.last!.split(separator: "/").compactMap { Double($0) }
            if parts.count >= 2, nums.count == 2, nums[1] > 0 {
                progress = nums[0] / nums[1]
                status = nums[1] <= 1 ? "\(parts[0])…" : "\(parts[0]): \(Int(nums[0])) of \(Int(nums[1]))"
            }
            return
        }
        log += line + "\n"
        if line.hasPrefix("Done:") { status = "Done! " + line.dropFirst(5).trimmingCharacters(in: .whitespaces) }
    }

    private func finish(code: Int32, file: URL) {
        process = nil
        if code == 0 {
            phase = .done
            progress = 1
            resultURL = file
            if !status.hasPrefix("Done!") { status = "Done!" }
        } else {
            phase = .failed
            status = "Failed: " + (log.split(separator: "\n").last.map(String.init) ?? "unknown error")
        }
    }

    func cancel() { process?.terminate() }
}

struct ReverseView: View {
    @ObservedObject var m: ReverseModel
    let openAny: () -> Void
    @State private var showLog = false
    @State private var showFXLib = false
    @State private var showRelink = false
    @State private var showAuto = false

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
        VStack(spacing: 14) {
            Card(title: "Input") {
                HStack {
                    Text("Session file:").font(.system(size: 12))
                    Text(m.ptxURL?.lastPathComponent ?? "—")
                        .font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Browse…") { openAny() }
                }
                if m.loading {
                    HStack(spacing: 6) { ProgressView().controlSize(.small); Detail(text: "Reading session…") }
                } else if let e = m.infoError {
                    Text(e).font(.system(size: 11)).foregroundStyle(.red)
                } else if let i = m.info {
                    if let a = i.app_info { Detail(text: a) }
                    Detail(text: i.rateLine)
                    let audioFiles = i.files_used ?? ((i.files ?? 0) - (i.video_files ?? 0))
                    Detail(text: "\(i.audio_tracks ?? i.tracks) tracks, \(i.clips_raw ?? i.clips) clips, \(i.fades_raw ?? i.fades ?? 0) fades, \(audioFiles) files, \(i.vol_points ?? 0) vol points, \(i.clip_gain_entries ?? 0) clip gain")
                    if (i.video_tracks ?? 0) > 0 || (i.video_files ?? 0) > 0 {
                        Detail(text: "Video: \(i.video_tracks ?? 0) track, \(i.video_clips ?? 0) clip")
                    }
                    if i.markers > 0 {
                        Detail(text: "\(i.markers) markers" + ((i.marker_regions ?? 0) > 0 ? ", \(i.marker_regions ?? 0) of them ranges (→ regions)" : ""))
                    }
                } else {
                    Detail(text: "Choose or drop a Pro Tools session (.ptx)")
                }
            }

            Card(title: "Preflight") {
                if let i = m.info {
                    let missing = i.missing_detail?.count ?? i.missing.count
                    if missing > 0 {
                        HStack {
                            Label("\(missing) missing of \(i.files ?? missing) files", systemImage: "exclamationmark.triangle.fill")
                                .font(.system(size: 12)).foregroundStyle(.orange)
                            Spacer()
                            Button("Relink…") { showRelink = true }
                        }
                    }
                    if i.skipped > 0 {
                        Label("\(i.skipped) clips skipped (unsupported clip format)", systemImage: "exclamationmark.triangle.fill")
                            .font(.system(size: 12)).foregroundStyle(.orange)
                    }
                    if (i.skipped_tracks ?? 0) > 0 {
                        let names = (i.skipped_detail ?? []).map { "\u{201C}\($0.name)\u{201D} (\($0.reason))" }
                        Label(names.isEmpty ? "\(i.skipped_tracks ?? 0) track(s) of unsupported types are skipped" : "Not transferred: " + names.joined(separator: ", "), systemImage: "info.circle.fill")
                            .font(.system(size: 12)).foregroundStyle(.blue)
                    }
                    if (i.track_markers ?? 0) > 0 {
                        Label("\(i.track_markers ?? 0) marker(s) on tracks become empty items on those tracks (Reaper has no track markers)", systemImage: "info.circle.fill")
                            .font(.system(size: 12)).foregroundStyle(.blue)
                    }
                    HStack(spacing: 10) {
                        Label("Ready to convert", systemImage: "checkmark.circle.fill")
                            .font(.system(size: 12)).foregroundStyle(.green)
                        Button { showAuto.toggle() } label: {
                            Label("What's handled automatically", systemImage: "info.circle")
                                .font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                        .popover(isPresented: $showAuto, arrowEdge: .bottom) {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("AUTOMATIC CONVERSIONS").font(.system(size: 10, weight: .bold)).foregroundStyle(.tertiary)
                                Detail(text: m.copyMedia ? "Source audio is copied unchanged into the project's Media folder." : "Nothing to process — the project references the original audio files directly.")
                                Text("COMPATIBILITY WARNINGS").font(.system(size: 10, weight: .bold)).foregroundStyle(.tertiary).padding(.top, 4)
                                Detail(text: missing > 0 ? "Missing audio files stay as offline items — use Relink… to locate them." : "No compatibility issues detected.")
                            }
                            .padding(14).frame(width: 300, alignment: .leading)
                        }
                    }
                } else {
                    Detail(text: "—")
                }
            }

            Card(title: "Output") {
                Detail(text: "Writes a Reaper project (.rpp) that points at the session's original audio files (nothing is copied or rendered).")
                TransferButton(allOn: m.opt.values.allSatisfy { $0 }) {
                    ForEach(Array(TrackDataGroups.all.enumerated()), id: \.offset) { gi, group in
                        if gi > 0 { Divider() }
                        ForEach(group, id: \.key) { it in
                            OptionRow(title: it.title, isOn: Binding(get: { it.supported && (m.opt[it.key] ?? false) }, set: { m.opt[it.key] = $0 }))
                                .disabled(!it.supported).help(it.supported ? "" : "Not supported yet")
                            if it.key == "playlists" && (m.opt["playlists"] ?? false) {
                                Picker("", selection: $m.altAsLanes) {
                                    Text("As Takes & Lanes").tag(true)
                                    Text("As muted tracks").tag(false)
                                }
                                .pickerStyle(.segmented).labelsHidden()
                            }
                        }
                    }
                }
                    Button("Plug-in library…") { showFXLib = true }.buttonStyle(.link).font(.system(size: 11))
                Picker("", selection: $m.copyMedia) {
                    Text("Link to source").tag(false)
                    Text("Copy Media (Standalone)").tag(true)
                }
                .pickerStyle(.segmented).labelsHidden()
                Detail(text: m.copyMedia
                       ? "Creates a project folder with the .rpp and a Media folder; the audio files used in the session are copied into it."
                       : "Writes only the .rpp; clips reference the audio files where they are now.")
                Toggle("Save in same folder as source", isOn: $m.sameFolder).toggleStyle(.checkbox).font(.system(size: 12))
                HStack {
                    Text("Output folder:").font(.system(size: 12))
                    Text(m.outputDir.lastPathComponent).font(.system(size: 12, weight: .medium))
                    Spacer()
                    Button("Browse…") { pickOutput() }.disabled(m.sameFolder)
                }
                HStack {
                    Text("Project name:").font(.system(size: 12))
                    TextField("", text: $m.projectName).textFieldStyle(.roundedBorder)
                }
            }

            VStack(spacing: 8) {
                if m.phase == .converting { ProgressView(value: m.progress).progressViewStyle(.linear) }
                HStack(spacing: 10) {
                    Button { showLog = true } label: { Image(systemName: "list.bullet") }.help("Show log")
                    Button {
                        switch m.phase {
                        case .done: if let u = m.resultURL { NSWorkspace.shared.activateFileViewerSelecting([u]) }
                        case .converting: m.cancel()
                        default: m.convert()
                        }
                    } label: {
                        Text(m.phase == .done ? "Reveal in Finder" : m.phase == .converting ? "Cancel" : "Convert to Reaper")
                            .frame(maxWidth: .infinity).padding(.vertical, 4)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(m.phase != .done && m.phase != .converting && !m.canConvert)
                }
                if !m.status.isEmpty {
                    Text(m.status).font(.system(size: 11)).foregroundStyle(m.phase == .failed ? .red : .secondary)
                        .frame(maxWidth: .infinity, alignment: .leading).lineLimit(2)
                }
                if m.phase == .done {
                    Button("Convert again") { m.phase = .idle; m.status = "" }.buttonStyle(.link).font(.system(size: 11))
                }
            }
        }
        .frame(width: 424)
        TracksCard(tracks: m.info?.track_list ?? [], selected: $m.selectedTracks).id(m.ptxURL)   // новый файл — фильтр и раскрытые папки с нуля
        }
        .fixedSize(horizontal: false, vertical: true)      // высоту задаёт левая колонка, список дорожек растягивается до неё
        .sheet(isPresented: $showLog) { LogSheet(text: m.log, close: { showLog = false }) }
        .sheet(isPresented: $showFXLib) { FXLibrarySheet(python: m.python, toolsDir: (m.script as NSString).deletingLastPathComponent, close: { showFXLib = false }) }
        .sheet(isPresented: $showRelink) {
            RelinkSheet(model: RelinkModel(
                missing: m.info?.missing_detail ?? [], python: m.python,
                formatHint: "Match Format (sample rate of the session)", durationHint: "Match Duration (long enough for the session)",
                candidateArgs: { [script = m.script, ptx = m.ptxURL, relink = m.dirArgs()] folders in
                    var a = [script, ptx?.path ?? "", "--candidates"]
                    for f in folders { a += ["--search", f.path] }
                    return a + relink.filter { $0 != "" } },
                onCommit: { map in m.relinkMap.merge(map) { _, new in new }; m.refreshInfo() }),
                        close: { showRelink = false })
        }
    }

    func pickSearchDir() {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        if p.runModal() == .OK, let u = p.url { m.addSearchDir(u) }
    }

    func pickOutput() {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        p.canCreateDirectories = true
        if p.runModal() == .OK, let u = p.url { m.customDir = u }
    }
}

enum AppMode { case none, forward, reverse }

/// Маршрутизация открытых/брошенных файлов. Ссылочный тип: замыкания drop не зависят от устаревших копий View.
@MainActor
final class Router: ObservableObject {
    let fwd = Model()
    let rev = ReverseModel()
    @Published var mode: AppMode = .none
    @Published var hint = ""

    /// Направление определяется расширением файла: .rpp → Reaper→Pro Tools, .ptx → Pro Tools→Reaper.
    /// Папку можно бросить целиком — внутри ищется проект (.rpp / .ptx), сначала с именем папки.
    func route(_ url: URL) {
        guard let target = resolve(url) else {
            var isDir: ObjCBool = false
            let dir = FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue
            hint = dir ? "No Reaper project (.rpp) or Pro Tools session (.ptx) found in the folder “\(url.lastPathComponent)”."
                       : "Unsupported file: \(url.lastPathComponent). Use a Reaper project (.rpp) or a Pro Tools session (.ptx)."
            return
        }
        hint = ""
        if target.pathExtension.lowercased() == "rpp" {
            fwd.load(target); mode = .forward
        } else {
            rev.load(target); mode = .reverse
        }
    }

    func resolve(_ url: URL) -> URL? {
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else { return nil }
        func supported(_ u: URL) -> Bool { ["rpp", "ptx"].contains(u.pathExtension.lowercased()) }
        if !isDir.boolValue { return supported(url) ? url : nil }
        func scan(_ dir: URL) -> [URL] {
            ((try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles])) ?? []).filter(supported)
        }
        var found = scan(url)
        if found.isEmpty {                      // на уровень глубже (например «Сессия/Project/Project.rpp»)
            for sub in (try? fm.contentsOfDirectory(at: url, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles])) ?? [] {
                if (try? sub.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true { found += scan(sub) }
            }
        }
        let want = url.lastPathComponent.lowercased()
        func date(_ u: URL) -> Date { (try? u.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast }
        return found.sorted {
            let m0 = $0.deletingPathExtension().lastPathComponent.lowercased() == want
            let m1 = $1.deletingPathExtension().lastPathComponent.lowercased() == want
            return m0 != m1 ? m0 : date($0) > date($1)
        }.first
    }
}

struct RootView: View {
    @StateObject private var r = Router()
    @State private var dropTargeted = false

    var title: String {
        switch r.mode {
        case .none: return "Reaper ⇄ Pro Tools"
        case .forward: return "Reaper → Pro Tools"
        case .reverse: return "Pro Tools → Reaper"
        }
    }

    var body: some View {
        VStack(spacing: 14) {
            VStack(spacing: 2) {
                Text(title).font(.system(size: 22, weight: .bold))
                Text("version \(appVersion)").font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            if !r.hint.isEmpty {
                Text(r.hint).font(.system(size: 11)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            switch r.mode {
            case .none:
                Card(title: "Input") {
                    HStack {
                        Text("Session file:").font(.system(size: 12))
                        Text("—").font(.system(size: 12, weight: .medium))
                        Spacer()
                        Button("Browse…") { openAny() }
                    }
                    Detail(text: "Choose or drop a Reaper project (.rpp) to convert it to Pro Tools, or a Pro Tools session (.ptx) to convert it to Reaper. A folder with a project inside also works. The direction is detected from the file.")
                }
            case .forward:
                ContentView(m: r.fwd, openAny: openAny)
            case .reverse:
                ReverseView(m: r.rev, openAny: openAny)
            }
        }
        .padding(18)
        .frame(width: r.mode == .none ? 460 : 460 + 14 + TracksCard.width)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay {
            if dropTargeted {
                RoundedRectangle(cornerRadius: 10).strokeBorder(Color.accentColor, lineWidth: 2).padding(4).allowsHitTesting(false)
            }
        }
        .dropDestination(for: URL.self) { urls, _ in
            guard !urls.isEmpty else { return false }
            let router = r
            // предпочитаем первый URL, из которого получится проект; иначе сообщаем про первый
            if let ok = urls.first(where: { router.resolve($0) != nil }) { router.route(ok) } else { router.route(urls[0]) }
            return true
        } isTargeted: { dropTargeted = $0 }
        .onOpenURL { url in r.route(url) }
        .onAppear {
            scheduleSnapshot()
            // Отладка: RTP_OPEN=/путь/файл (.rpp или .ptx)
            let env = ProcessInfo.processInfo.environment
            if let path = env["RTP_OPEN"] ?? env["RTP_OPEN_PTX"] { r.route(URL(fileURLWithPath: path)) }
        }
    }

    func openAny() {
        let p = NSOpenPanel()
        p.allowedContentTypes = [UTType(filenameExtension: "rpp") ?? .data, UTType(filenameExtension: "ptx") ?? .data]
        p.canChooseDirectories = true
        if p.runModal() == .OK, let u = p.url { r.route(u) }
    }
}

/// Расширение Reaper (File → Import Pro Tools Session…): лежит в Resources/ReaperExtension, ставится в UserPlugins Reaper.
enum ReaperExtension {
    static let fileName = "reaper_pt_session_import.dylib"
    static var bundled: URL? {
        Bundle.main.resourceURL?.appendingPathComponent("ReaperExtension/" + fileName)
    }
    static var installedDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/REAPER/UserPlugins")
    }
    static var installed: URL { installedDir.appendingPathComponent(fileName) }

    @discardableResult
    static func install() throws -> URL {
        let fm = FileManager.default
        guard let src = bundled, fm.fileExists(atPath: src.path) else {
            throw NSError(domain: "ReaperExtension", code: 1, userInfo: [NSLocalizedDescriptionKey: "The extension is not part of this build."])
        }
        try fm.createDirectory(at: installedDir, withIntermediateDirectories: true)
        try? fm.removeItem(at: installed)
        try fm.copyItem(at: src, to: installed)
        removexattr(installed.path, "com.apple.quarantine", 0)
        return installed
    }

    /// После обновления приложения: если расширение уже стоит, а версия в приложении другая — молча заменяем свою же копию.
    static func refreshIfInstalled() {
        let fm = FileManager.default
        guard let src = bundled, fm.fileExists(atPath: installed.path),
              let a = try? Data(contentsOf: src), let b = try? Data(contentsOf: installed), a != b else { return }
        _ = try? install()
    }

    static func installWithAlert() {
        let alert = NSAlert()
        do {
            try install()
            alert.messageText = "Reaper extension installed"
            alert.informativeText = "Restart Reaper, then use File → Import Pro Tools Session…\n\nInstalled to:\n\(installed.path)"
        } catch {
            alert.alertStyle = .warning
            alert.messageText = "Could not install the Reaper extension"
            alert.informativeText = error.localizedDescription
        }
        alert.runModal()
    }

    static func uninstallWithAlert() {
        let alert = NSAlert()
        if FileManager.default.fileExists(atPath: installed.path) {
            try? FileManager.default.removeItem(at: installed)
            alert.messageText = "Reaper extension removed"
            alert.informativeText = "It disappears from Reaper's File menu after Reaper restarts."
        } else {
            alert.messageText = "The Reaper extension is not installed"
        }
        alert.runModal()
    }
}

@main
struct ReaperToProToolsApp: App {
    @StateObject private var updater = Updater()

    init() { ReaperExtension.refreshIfInstalled() }

    var body: some Scene {
        WindowGroup {
            RootView().modifier(UpdateUI(u: updater))
        }
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Check for Updates…") { updater.check(manual: true) }
                Divider()
                Button("Install Reaper Extension…") { ReaperExtension.installWithAlert() }
                Button("Remove Reaper Extension") { ReaperExtension.uninstallWithAlert() }
            }
        }
    }
}
