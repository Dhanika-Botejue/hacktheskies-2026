import SwiftUI

/// Top-down drawing of the walked path. Same look as web/js/trailview.js.
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

            // Follow the walker, or fit the whole path (at least 8 m across).
            let cx: Double, cy: Double, scale: Double
            if follow {
                cx = pos.x
                cy = pos.y
                scale = min(w, h) / 12
            } else {
                let minX = points.map(\.x).min()!, maxX = points.map(\.x).max()!
                let minY = points.map(\.y).min()!, maxY = points.map(\.y).max()!
                cx = (minX + maxX) / 2
                cy = (minY + maxY) / 2
                scale = min(w / max(8, maxX - minX + 2), h / max(8, maxY - minY + 2))
            }
            func screen(_ p: TrailPoint) -> CGPoint {
                CGPoint(x: w / 2 + (p.x - cx) * scale, y: h / 2 + (p.y - cy) * scale)
            }

            // 1 m grid
            var grid = Path()
            for gx in Int(floor(cx - w / 2 / scale))...Int(ceil(cx + w / 2 / scale)) {
                let sx = screen(TrailPoint(x: Double(gx), y: 0)).x
                grid.move(to: CGPoint(x: sx, y: 0))
                grid.addLine(to: CGPoint(x: sx, y: h))
            }
            for gy in Int(floor(cy - h / 2 / scale))...Int(ceil(cy + h / 2 / scale)) {
                let sy = screen(TrailPoint(x: 0, y: Double(gy))).y
                grid.move(to: CGPoint(x: 0, y: sy))
                grid.addLine(to: CGPoint(x: w, y: sy))
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

            // You + facing direction
            if let heading {
                let me = screen(pos), a = heading * .pi / 180, r = 9.0
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
