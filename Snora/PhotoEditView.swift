import SwiftUI
import UIKit

/// ── annotate & edit a still ────────────────────────────────────────────
///
/// Pull up any picture (a studio still, or the last frame of a clip), draw a
/// mark over the part that should change, then say or type what you want. The
/// marked-up image goes to Qwen-Image-2.1 as a reference and the instruction is
/// framed as a circled-region edit: change the marked area, hold everything
/// else exactly. The result can be saved, edited again, or sent to Create as
/// the first frame of the next shot.
///
/// Everything here is local — the picture never leaves the tailnet.
struct PhotoEditView: View {
    let initialImage: UIImage
    var sourceRecord: PhotoRecord? = nil

    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var store = PhotoStore.shared
    @ObservedObject private var bridge = PhotoBridge.shared
    @StateObject private var ear = AssistEar()

    @State private var image: UIImage
    @State private var strokes: [[CGPoint]] = []
    @State private var current: [CGPoint] = []
    @State private var canvasSize: CGSize = .zero
    @State private var instruction = ""
    @State private var busy = false
    @State private var result: UIImage?
    @State private var note = ""
    @FocusState private var focus: Bool

    /// the mark colour the model was trained to read as "change this"
    private let mark = Color(red: 0.96, green: 0.16, blue: 0.10)
    private let markUI = UIColor(red: 0.96, green: 0.16, blue: 0.10, alpha: 1)

    init(initialImage: UIImage, sourceRecord: PhotoRecord? = nil) {
        self.initialImage = initialImage
        self.sourceRecord = sourceRecord
        _image = State(initialValue: initialImage)
    }

    var body: some View {
        NavigationStack {
            ZStack {
                SnoraBackground()
                ScrollView {
                    VStack(spacing: 14) {
                        canvasCard
                        sayCard
                        if let result { resultCard(result) }
                        actions
                        if !note.isEmpty {
                            Text(note)
                                .font(.footnote)
                                .foregroundStyle(note.hasPrefix("done") ? Theme.crt : Theme.ember)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 4)
                        }
                    }
                    .padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationTitle("Edit")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { dismiss() } label: {
                        Text("CLOSE")
                            .font(.snoraTag).kerning(1.2)
                            .foregroundStyle(Theme.cream)
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .background(Capsule().fill(Theme.card.opacity(0.9)))
                            .overlay(Capsule().stroke(Theme.cardStroke, lineWidth: 1))
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    if !strokes.isEmpty || !current.isEmpty {
                        Button { strokes.removeAll(); current = [] } label: {
                            Text("CLEAR MARKS")
                                .font(.snoraTag).kerning(1.2)
                                .foregroundStyle(Theme.ember)
                                .padding(.horizontal, 14).padding(.vertical, 8)
                                .background(Capsule().fill(Theme.card.opacity(0.9)))
                                .overlay(Capsule().stroke(Theme.ember.opacity(0.6), lineWidth: 1))
                        }
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
        .onDisappear { ear.cancel() }
    }

    // ── the picture you draw on ──────────────────────────────────────────
    private var canvasCard: some View {
        SectionCard(title: "circle what should change",
                    footnote: "draw right on the picture — a ring, an arrow, a scribble. then say or type what you want.") {
            GeometryReader { geo in
                ZStack {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                    Canvas { ctx, size in
                        let lw = max(3, size.width * 0.012)
                        var all = strokes
                        if current.count > 1 { all.append(current) }
                        for s in all {
                            guard let first = s.first else { continue }
                            var p = Path()
                            p.move(to: first)
                            for pt in s.dropFirst() { p.addLine(to: pt) }
                            ctx.stroke(p, with: .color(mark),
                                       style: StrokeStyle(lineWidth: lw, lineCap: .round, lineJoin: .round))
                        }
                    }
                    .contentShape(Rectangle())
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { v in current.append(v.location) }
                            .onEnded { _ in
                                if current.count > 1 { strokes.append(current) }
                                current = []
                            }
                    )
                }
                .onAppear { canvasSize = geo.size }
                .onChange(of: geo.size) { _, s in canvasSize = s }
            }
            .aspectRatio(image.size.width / max(1, image.size.height), contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))

            HStack(spacing: 10) {
                Button {
                    if !current.isEmpty { current = [] }
                    else if !strokes.isEmpty { strokes.removeLast() }
                } label: {
                    Label("undo", systemImage: "arrow.uturn.backward")
                }
                .buttonStyle(GhostButtonStyle())
                .disabled(strokes.isEmpty && current.isEmpty)
                Spacer()
                Text("\(strokes.count) mark\(strokes.count == 1 ? "" : "s")")
                    .font(.snoraLabel).foregroundStyle(Theme.sand.opacity(0.85))
            }
        }
    }

    // ── say or type the change ───────────────────────────────────────────
    private var sayCard: some View {
        SectionCard(title: "what should change",
                    footnote: "say it out loud, or type it. name only the thing you marked — everything else is kept.") {
            ZStack(alignment: .topLeading) {
                if instruction.isEmpty {
                    Text("make the circled jacket red, add a hat, take out the sign…")
                        .font(.body).foregroundStyle(Theme.sand.opacity(0.5))
                        .padding(.top, 8).padding(.leading, 5)
                        .allowsHitTesting(false)
                }
                TextEditor(text: $instruction)
                    .scrollContentBackground(.hidden)
                    .foregroundStyle(Theme.cream)
                    .frame(minHeight: 74)
                    .focused($focus)
            }
            Button { Task { await toggleEar() } } label: {
                HStack(spacing: 8) {
                    Image(systemName: ear.listening ? "waveform" : "mic.fill")
                    Text(ear.listening ? "STOP — I'M LISTENING" : "SAY IT")
                        .kerning(1.2)
                }
                .font(.system(.body, design: .monospaced).weight(.bold))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .background(Capsule().fill(ear.listening ? Theme.ember : Theme.signal.opacity(0.9)))
                .foregroundStyle(Theme.cream)
            }
            .buttonStyle(.plain)
            if ear.listening, !ear.partial.isEmpty {
                Text(ear.partial).font(.footnote).foregroundStyle(Theme.sand).lineLimit(3)
            }
            if let h = ear.hint, !h.isEmpty {
                Text(h).font(.caption).foregroundStyle(Theme.sand.opacity(0.8))
            }
        }
    }

    private func toggleEar() async {
        if ear.listening {
            if let t = await ear.finish() {
                instruction = instruction.isEmpty ? t : instruction + " " + t
            }
            if let d = ear.diagnostic, instruction.isEmpty {
                note = d
            }
        } else {
            note = ""
            focus = false
            try? await ear.begin()
        }
    }

    // ── result preview ───────────────────────────────────────────────────
    private func resultCard(_ img: UIImage) -> some View {
        SectionCard(title: "the change") {
            Image(uiImage: img)
                .resizable()
                .scaledToFit()
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))
            HStack(spacing: 10) {
                Button { save(img) } label: {
                    Label("SAVE TO PHOTOS", systemImage: "square.and.arrow.down")
                }
                .buttonStyle(GhostButtonStyle())
                Button {
                    // keep editing from this result
                    image = img
                    strokes = []; current = []
                    result = nil
                    note = "editing the new picture — mark the next change"
                } label: {
                    Label("AGAIN", systemImage: "arrow.triangle.2.circlepath")
                }
                .buttonStyle(GhostButtonStyle())
            }
        }
    }

    // ── the big buttons ──────────────────────────────────────────────────
    private var actions: some View {
        VStack(spacing: 10) {
            Button { applyChange() } label: {
                HStack(spacing: 8) {
                    if busy { ProgressView().tint(Theme.cream) }
                    else { Image(systemName: "wand.and.stars") }
                    Text(busy ? "WORKING…" : "MAKE THE CHANGE").kerning(1.4)
                }
                .font(.system(.body, design: .monospaced).weight(.bold))
            }
            .buttonStyle(SignalButtonStyle())
            .disabled(busy || instruction.trimmingCharacters(in: .whitespaces).isEmpty)

            Button { continueAsNextShot() } label: {
                Label("USE AS THE NEXT SHOT", systemImage: "video.badge.plus")
                    .kerning(1.2)
                    .font(.system(.body, design: .monospaced).weight(.bold))
            }
            .buttonStyle(GhostButtonStyle())

            if sourceRecord != nil {
                Text("or tap SAVE above to keep the new picture")
                    .font(.caption).foregroundStyle(Theme.sand.opacity(0.8))
            }
        }
    }

    // ── run the edit ─────────────────────────────────────────────────────
    private func applyChange() {
        let ask = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !ask.isEmpty else { note = "say or type what should change first"; return }
        guard let annotated = composite() else { note = "couldn't prepare the picture"; return }
        busy = true
        note = ""
        Task {
            do {
                let res = try await StudioAPI.generateQwenEdit(image: annotated, prompt: editPrompt(ask))
                let img = try await StudioAPI.fetchImage(path: res.image)
                result = img
                note = "done — save it, or edit again"
            } catch {
                note = error.localizedDescription
            }
            busy = false
        }
    }

    /// the circled-region edit contract, spelled the way Qwen reads it: lead with
    /// the operation, name only the marked area, then one blanket preservation
    /// clause (concrete description of kept things makes them drift).
    private func editPrompt(_ ask: String) -> String {
        let marks = strokes.isEmpty ? "the area marked in bright red" : "the area marked with the bright red circle or mark"
        return "Edit the attached image. Change ONLY \(marks): \(ask). "
            + "Keep everything else in the picture exactly as it is — the same person, "
            + "the same objects, background, text, lighting and composition."
    }

    /// flatten the image + marks into one PNG at the picture's own resolution
    private func composite() -> UIImage? {
        guard canvasSize.width > 1, canvasSize.height > 1 else { return nil }
        let fmt = UIGraphicsImageRendererFormat.default()
        fmt.scale = 1                      // keep the pixel size == the picture's size
        let r = UIGraphicsImageRenderer(size: image.size, format: fmt)
        let sx = image.size.width / canvasSize.width
        let sy = image.size.height / canvasSize.height
        let lw = max(4, image.size.width * 0.014)
        return r.image { ctx in
            image.draw(in: CGRect(origin: .zero, size: image.size))
            let cg = ctx.cgContext
            cg.setStrokeColor(markUI.cgColor)
            cg.setLineWidth(lw)
            cg.setLineCap(.round)
            cg.setLineJoin(.round)
            for s in strokes {
                guard let first = s.first else { continue }
                cg.move(to: CGPoint(x: first.x * sx, y: first.y * sy))
                for pt in s.dropFirst() { cg.addLine(to: CGPoint(x: pt.x * sx, y: pt.y * sy)) }
                cg.strokePath()
            }
        }
    }

    private func save(_ img: UIImage) {
        PhotoStore.shared.add(remotePath: "", prompt: instruction.isEmpty ? "edited picture" : instruction,
                              engine: "QWEN EDIT", image: img)
        note = "saved to your photos"
    }

    /// hand the picture to Create as the first frame of the next clip, and switch tabs
    private func continueAsNextShot() {
        bridge.pendingReference = image
        dismiss()
    }
}
