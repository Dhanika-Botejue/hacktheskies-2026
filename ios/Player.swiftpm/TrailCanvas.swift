import SwiftUI

/// Top-down drawing of the walked path. Same look as web/js/trailview.js, but heading-up:
/// the map turns around you so your arrow always points to the top of the screen.
struct TrailCanvas: View {
    var path: [TrailPoint]
    var pos: TrailPoint
    var heading: Double?
    var follow: Bool

    static let blue = Color(red: 0.16, green: 0.71, blue: 0.96)
    static let green = Color(red: 0.46, green: 0.69, blue: 0.25)
    static let yellow = Color(red: 1.0, green: 0.79, blue: 0.08)

    var body: some View {
        Canvas { ctx, size in
            let w = size.width, h = size.height
            let points = path + [pos]

            // Turn the map by -heading so the way you face is up.
            let turn = (heading ?? 0) * .pi / 180
            func rotated(_ p: TrailPoint) -> TrailPoint {
                TrailPoint(x: p.x * cos(turn) + p.y * sin(turn), y: -p.x * sin(turn) + p.y * cos(turn))
            }

            // Follow the walker, or fit the whole path (at least 8 m across).
            let cx: Double, cy: Double, scale: Double
            if follow {
                let me = rotated(pos)
                cx = me.x
                cy = me.y
                scale = min(w, h) / 12
            } else {
                let turned = points.map(rotated)
                let minX = turned.map(\.x).min()!, maxX = turned.map(\.x).max()!
                let minY = turned.map(\.y).min()!, maxY = turned.map(\.y).max()!
                cx = (minX + maxX) / 2
                cy = (minY + maxY) / 2
                scale = min(w / max(8, maxX - minX + 2), h / max(8, maxY - minY + 2))
            }
            func screen(_ p: TrailPoint) -> CGPoint {
                let r = rotated(p)
                return CGPoint(x: w / 2 + (r.x - cx) * scale, y: h / 2 + (r.y - cy) * scale)
            }

            // 1 m grid, long enough to cover the screen at any rotation
            let center = TrailPoint(x: cx * cos(turn) - cy * sin(turn), y: cx * sin(turn) + cy * cos(turn))
            let reach = hypot(w, h) / 2 / scale + 1
            var grid = Path()
            for gx in Int(floor(center.x - reach))...Int(ceil(center.x + reach)) {
                grid.move(to: screen(TrailPoint(x: Double(gx), y: center.y - reach)))
                grid.addLine(to: screen(TrailPoint(x: Double(gx), y: center.y + reach)))
            }
            for gy in Int(floor(center.y - reach))...Int(ceil(center.y + reach)) {
                grid.move(to: screen(TrailPoint(x: center.x - reach, y: Double(gy))))
                grid.addLine(to: screen(TrailPoint(x: center.x + reach, y: Double(gy))))
            }
            ctx.stroke(grid, with: .color(.white.opacity(0.08)), lineWidth: 1)

            // Path
            var line = Path()
            for (i, p) in points.enumerated() {
                if i == 0 { line.move(to: screen(p)) } else { line.addLine(to: screen(p)) }
            }
            ctx.stroke(line, with: .color(Self.blue), style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))

            // Start marker
            if let first = path.first {
                let s = screen(first)
                ctx.fill(Path(ellipseIn: CGRect(x: s.x - 6, y: s.y - 6, width: 12, height: 12)), with: .color(Self.green))
            }

            // You, always facing up
            if heading != nil {
                let me = screen(pos), a = 0.0, r = 9.0
                var arrow = Path()
                arrow.move(to: CGPoint(x: me.x + sin(a) * r * 2.4, y: me.y - cos(a) * r * 2.4))
                arrow.addLine(to: CGPoint(x: me.x + sin(a + 2.4) * r, y: me.y - cos(a + 2.4) * r))
                arrow.addLine(to: CGPoint(x: me.x + sin(a - 2.4) * r, y: me.y - cos(a - 2.4) * r))
                arrow.closeSubpath()
                arrow.addEllipse(in: CGRect(x: me.x - r, y: me.y - r, width: 2 * r, height: 2 * r))
                ctx.fill(arrow, with: .color(Self.yellow))
            }

            // Scale bar
            ctx.fill(Path(CGRect(x: 12, y: h - 14, width: scale, height: 2)), with: .color(.gray))
            ctx.draw(Text("1 m").font(.caption2).foregroundStyle(.gray), at: CGPoint(x: 12, y: h - 24), anchor: .leading)
        }
    }
}
