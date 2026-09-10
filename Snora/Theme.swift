import SwiftUI

/// ── "signal": retro-broadcast palette ─────────────────────────────────
/// warm CRT charcoal, one signal red, cream ink, terminal green for done.
/// type voice: monospaced uppercase labels, like an old TV on-screen menu.
enum Theme {
    static let bgTop = Color(red: 0.110, green: 0.090, blue: 0.075) // tube warmed up
    static let bgBottom = Color(red: 0.050, green: 0.040, blue: 0.033) // deep
    static let card = Color(red: 0.118, green: 0.098, blue: 0.084) // graphite panel
    static let cardStroke = Color.white.opacity(0.07)
    static let signal = Color(red: 0.945, green: 0.240, blue: 0.170) // transmitter red
    static let signalHot = Color(red: 1.000, green: 0.355, blue: 0.275) // top of the badge
    static let ink = Color(red: 0.075, green: 0.060, blue: 0.050) // text on red
    static let cream = Color(red: 0.957, green: 0.933, blue: 0.886) // primary ink
    static let sand = Color(red: 0.640, green: 0.590, blue: 0.535) // secondary ink
    static let crt = Color(red: 0.360, green: 0.855, blue: 0.470) // terminal green
    static let ember = Color(red: 1.000, green: 0.505, blue: 0.420) // failed
}

extension Font {
    /// the TV on-screen-menu voice
    static let snoraTag = Font.system(.caption2, design: .monospaced).weight(.bold)
    static let snoraLabel = Font.system(.caption2, design: .monospaced).weight(.semibold)
}

/// ── background: charcoal tube with a red glow and faint scanlines ─────
struct SnoraBackground: View {
    var body: some View {
        ZStack {
            LinearGradient(colors: [Theme.bgTop, Theme.bgBottom],
                           startPoint: .top, endPoint: .bottom)
            RadialGradient(colors: [Theme.signal.opacity(0.16), .clear],
                           center: UnitPoint(x: 0.5, y: -0.12),
                           startRadius: 10, endRadius: 340)
            Scanlines()
        }
        .ignoresSafeArea()
    }
}

struct Scanlines: View {
    var body: some View {
        Canvas { ctx, size in
            var y: CGFloat = 0
            while y < size.height {
                ctx.fill(Path(CGRect(x: 0, y: y, width: size.width, height: 1)),
                         with: .color(.black.opacity(0.18)))
                y += 4
            }
        }
        .allowsHitTesting(false)
    }
}

/// ── the panel every block of content sits on ──────────────────────────
struct CardBackground: ViewModifier {
    var corner: CGFloat = 16
    func body(content: Content) -> some View {
        content
            .background(Theme.card, in: RoundedRectangle(cornerRadius: corner))
            .overlay(RoundedRectangle(cornerRadius: corner).stroke(Theme.cardStroke, lineWidth: 1))
    }
}

extension View {
    func snoraCard(corner: CGFloat = 16) -> some View {
        modifier(CardBackground(corner: corner))
    }
}

/// ── labelled card with a mono uppercase heading + optional footnote ───
struct SectionCard<Content: View>: View {
    var title: String
    var footnote: String? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Rectangle().fill(Theme.signal).frame(width: 14, height: 2)
                Text(title).font(.snoraTag)
                    .foregroundStyle(Theme.sand)
                    .kerning(1.6)
                    .textCase(.uppercase)
            }
            content()
            if let f = footnote {
                Text(f).font(.footnote).foregroundStyle(Theme.sand.opacity(0.85))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .snoraCard()
    }
}

/// ── chip selector (replaces the stock segmented pickers) ──────────────
struct Chip: View {
    var title: String
    var selected: Bool
    var compact: Bool = false
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(compact ? .snoraTag : .subheadline.weight(selected ? .semibold : .regular))
                .foregroundStyle(selected ? Theme.cream : Theme.sand)
                .padding(.horizontal, compact ? 11 : 14)
                .padding(.vertical, compact ? 6 : 8)
                .background {
                    if selected {
                        Capsule()
                            .fill(Theme.signal)
                            .overlay(Capsule().stroke(Theme.signalHot.opacity(0.8), lineWidth: 1))
                            .shadow(color: Theme.signal.opacity(0.45), radius: 7, y: 2)
                    } else {
                        Capsule().fill(Theme.card.opacity(0.7))
                            .overlay(Capsule().stroke(Theme.cardStroke, lineWidth: 1))
                    }
                }
        }
        .buttonStyle(.plain)
    }
}

struct ChipRow<T: Hashable>: View {
    var label: String
    var options: [(label: String, value: T)]
    @Binding var selection: T

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(label)
                .font(.snoraLabel)
                .foregroundStyle(Theme.sand)
                .kerning(1.0)
                .textCase(.uppercase)
            HStack(spacing: 8) {
                ForEach(options, id: \.value) { opt in
                    Chip(title: opt.label, selected: selection == opt.value) {
                        withAnimation(.spring(duration: 0.25)) { selection = opt.value }
                    }
                }
                Spacer(minLength: 0)
            }
        }
    }
}

/// ── selectable option card (two-line, for photo means etc.) ───────────
struct OptionRow: View {
    var title: String
    var subtitle: String
    var selected: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.subheadline.weight(.medium))
                        .foregroundStyle(selected ? Theme.cream : Theme.sand)
                    Text(subtitle).font(.caption).foregroundStyle(Theme.sand.opacity(0.75))
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(selected ? Theme.signal : Theme.sand.opacity(0.4))
                    .font(.system(size: 20))
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 12).fill(selected ? Theme.signal.opacity(0.10) : .clear))
            .overlay(RoundedRectangle(cornerRadius: 12)
                .stroke(selected ? Theme.signal.opacity(0.55) : Theme.cardStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }
}

/// ── the big transmitter button ─────────────────────────────────────────
struct SignalButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.bold))
            .padding(.vertical, 15)
            .frame(maxWidth: .infinity)
            .foregroundStyle(Theme.cream)
            .background {
                RoundedRectangle(cornerRadius: 14)
                    .fill(LinearGradient(colors: [Theme.signalHot, Theme.signal],
                                         startPoint: .top, endPoint: .bottom))
                    .overlay(RoundedRectangle(cornerRadius: 14)
                        .stroke(Theme.cream.opacity(0.25), lineWidth: 1))
                    .shadow(color: Theme.signal.opacity(configuration.isPressed ? 0.15 : 0.45),
                            radius: configuration.isPressed ? 4 : 14, y: 5)
            }
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.spring(duration: 0.3), value: configuration.isPressed)
    }
}

/// ── ghost button (secondary actions: take photo, record, …) ───────────
struct GhostButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.medium))
            .foregroundStyle(Theme.cream)
            .padding(.vertical, 10).padding(.horizontal, 14)
            .frame(maxWidth: .infinity)
            .background(Capsule().fill(Theme.card.opacity(0.7)))
            .overlay(Capsule().stroke(Theme.cardStroke, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.6 : 1)
    }
}

/// ── ember button (the danger actions you can actually see: delete, stop) ──
struct EmberButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(Theme.ember)
            .padding(.vertical, 12).padding(.horizontal, 14)
            .frame(maxWidth: .infinity)
            .background(Capsule().fill(Theme.ember.opacity(configuration.isPressed ? 0.24 : 0.10)))
            .overlay(Capsule().stroke(Theme.ember.opacity(0.7), lineWidth: 1))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

/// ── tiny mono meta tag (quality / shape / length on reel cards) ───────
struct MetaTag: View {
    var text: String
    var body: some View {
        Text(text)
            .font(.snoraTag)
            .foregroundStyle(Theme.sand)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(Capsule().fill(Theme.bgBottom.opacity(0.6)))
            .overlay(Capsule().stroke(Theme.cardStroke, lineWidth: 1))
    }
}

/// ── blinking transmit dot ─────────────────────────────────────────────
struct BlinkDot: View {
    var color: Color = Theme.signal
    @State private var on = false
    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 7, height: 7)
            .opacity(on ? 1 : 0.25)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true)) { on = true }
            }
    }
}

/// ── status pill (mono, broadcast style) ───────────────────────────────
struct StatusPill: View {
    let status: String
    var body: some View {
        let (t, c): (String, Color) =
            status == "done" ? ("DONE", Theme.crt)
                : status == "failed" ? ("FAILED", Theme.ember)
                : status == "rendering" ? ("RENDERING", Theme.signal)
                : ("QUEUED", Theme.sand)
        HStack(spacing: 5) {
            if status == "rendering" {
                BlinkDot(color: c)
            }
            Text(t).font(.snoraTag)
        }
        .padding(.horizontal, 8).padding(.vertical, 3.5)
        .background(Capsule().fill(c.opacity(0.14)))
        .overlay(Capsule().stroke(c.opacity(0.5), lineWidth: 1))
        .foregroundStyle(c)
    }
}

/// ── the mark: red play badge (empty states, headers) ──────────────────
struct PlayBadge: View {
    var size: CGFloat = 96
    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.28)
            .fill(LinearGradient(colors: [Theme.signalHot, Theme.signal],
                                 startPoint: .top, endPoint: .bottom))
            .overlay(RoundedRectangle(cornerRadius: size * 0.28)
                .stroke(Theme.cream.opacity(0.3), lineWidth: 1.5))
            .overlay(
                Image(systemName: "play.fill")
                    .font(.system(size: size * 0.38))
                    .foregroundStyle(Theme.cream)
                    .offset(x: size * 0.03)
            )
            .frame(width: size * 1.28, height: size * 0.9)
            .shadow(color: Theme.signal.opacity(0.4), radius: 18, y: 8)
    }
}
