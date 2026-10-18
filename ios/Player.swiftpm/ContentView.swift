import ARKit
import SwiftUI

@main
struct PlayerApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .preferredColorScheme(.dark)
        }
    }
}

struct ContentView: View {
    @State private var tracker = Tracker()
    @State private var client = GameClient()
    @State private var combat = CombatClient()
    @State private var follow = true
    @State private var joined = false
    @State private var joinError: String?
    @AppStorage("server") private var server = "10.110.0.189:8443"
    @AppStorage("name") private var name = ""
    @AppStorage("tag") private var tag = 1  // your player number: the one on your chest tag (or scanned as, in clothes mode)
    @AppStorage("team") private var team = "red"

    private static let teamColors = ["red": Color(red: 1, green: 0.3, blue: 0.37), "blue": Color(red: 0.3, green: 0.64, blue: 1)]

    private static let panel = Color(red: 0.11, green: 0.12, blue: 0.15)

    var body: some View {
        ZStack {
            CameraView(tracker: tracker).ignoresSafeArea()

            VStack(spacing: 0) {
                gameBar
                if joined { combatBar }
                HStack(spacing: 1) {
                    stat("Distance", String(format: "%.1f m", tracker.distance))
                    stat("Speed", String(format: "%.1f m/s", tracker.speed))
                    stat("Heading", tracker.heading.map { "\(Int($0.rounded()))°" } ?? "–")
                }
                .background(Color.gray.opacity(0.3))

                ZStack(alignment: .top) {
                    TrailCanvas(path: tracker.path, pos: tracker.pos, heading: tracker.heading, follow: follow)
                        .background(Color(red: 0.08, green: 0.09, blue: 0.1).opacity(0.82))
                    Text(tracker.status)
                        .font(.subheadline.bold())
                        .foregroundStyle(.black)
                        .frame(maxWidth: .infinity)
                        .padding(8)
                        .background(tracker.tracking ? TrailCanvas.green : Color.red, in: RoundedRectangle(cornerRadius: 8))
                        .padding(10)
                }

                HStack(spacing: 8) {
                    Button("Reset trail") { tracker.reset() }
                        .buttonStyle(.bordered)
                    Button(follow ? "Following" : "Show all") { follow.toggle() }
                        .buttonStyle(.borderedProminent)
                }
                .frame(maxWidth: .infinity)
                .padding(12)
                .background(Self.panel)
            }
        }
        .overlay {
            // Hit feedback you can catch from the corner of your eye.
            flashColor.opacity(combat.flash == nil ? 0 : 0.45)
                .ignoresSafeArea()
                .allowsHitTesting(false)
                .animation(.easeOut(duration: 0.35), value: combat.flash)
        }
        .onAppear {
            tracker.onPose = { [client] x, z, yaw, tracking, epoch, time in
                client.cameraPose(x: x, z: z, yaw: yaw, tracking: tracking, epoch: epoch, time: time)
            }
            // The same camera frames go to the shooting server: one camera does both jobs.
            tracker.onFrame = { [combat] frame in combat.offer(frame) }
            tracker.start()
            UIApplication.shared.isIdleTimerDisabled = true  // screen stays on while strapped to the chest
        }
    }

    @ViewBuilder private var gameBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            if joined {
                HStack {
                    Circle().fill(client.connected ? TrailCanvas.green : Color.red).frame(width: 8, height: 8)
                    Text(client.connected ? (client.placed ? "In game · \(client.source)" : "Connected · ask the host to place you")
                                          : "Connecting to \(server)…")
                        .font(.footnote)
                    Spacer()
                    Text("\(client.steps) steps").font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                    Button("Leave") {
                        client.disconnect()
                        combat.disconnect()
                        joined = false
                    }
                    .font(.footnote)
                }
            } else {
                Text("Join the game").font(.headline)
                HStack {
                    TextField("Your name", text: $name).textFieldStyle(.roundedBorder)
                    TextField("Laptop, e.g. 10.110.0.139:8443", text: $server)
                        .textFieldStyle(.roundedBorder)
                        .keyboardType(.numbersAndPunctuation)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                }
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text("Tag #").font(.footnote).foregroundStyle(.secondary)
                            TextField("1–34", value: $tag, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.numberPad)
                                .frame(width: 56)
                        }
                        Text("the number on your chest tag").font(.caption2).foregroundStyle(.secondary)
                    }
                    Picker("Team", selection: $team) {
                        Text("Red").tag("red")
                        Text("Blue").tag("blue")
                    }
                    .pickerStyle(.segmented)
                    Button("Join") { join() }
                        .buttonStyle(.borderedProminent)
                }
                if let error = joinError ?? client.error ?? combat.error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
            }
        }
        .padding(10)
        .background(Self.panel)
    }

    private func join() {
        guard (1...34).contains(tag) else {
            joinError = "Tag # is the number on your printed chest tag (1–34)"
            return
        }
        joinError = nil
        client.connect(server: server, name: name.isEmpty ? "iPhone" : name, tag: tag, team: team)
        guard client.error == nil else { return }  // e.g. a bad address: shown here on the join screen
        combat.connect(server: server, tag: tag, team: team)
        joined = true
    }

    /// Shooting: hit points, gun and what the camera sees. Spoken aloud too.
    @ViewBuilder private var combatBar: some View {
        let color = Self.teamColors[team] ?? .white
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Circle().fill(combat.registered ? TrailCanvas.green : Color.red).frame(width: 8, height: 8)
                Text("#\(tag) \(team.capitalized)").font(.footnote.bold()).foregroundStyle(color)
                Text(String(repeating: "♥", count: max(0, combat.hp)) + String(repeating: "♡", count: max(0, combat.maxHp - combat.hp)))
                    .foregroundStyle(Color(red: 1, green: 0.42, blue: 0.5))
                Text(combat.alive ? gunText : combat.respawnMode == "base" ? "DOWN · TO BASE" : "DOWN").font(.footnote.bold())
                    .foregroundStyle(combat.alive && combat.gun == "ready" ? TrailCanvas.green : TrailCanvas.yellow)
                Spacer()
                Text(combat.score).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
            }
            HStack {
                Text(combat.error ?? combat.status).font(.footnote).foregroundStyle(combat.error == nil ? Color.primary : .red)
                Spacer()
                Text("🎤 \(combat.mic)").font(.caption2).foregroundStyle(.secondary)
            }
            if combat.needsScan {
                Text("Not scanned yet: others can't hit you until the host scans you at the scan station")
                    .font(.caption.bold())
                    .foregroundStyle(TrailCanvas.yellow)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Self.panel.opacity(0.95))
        .overlay(alignment: .leading) { Rectangle().fill(color).frame(width: 3) }
    }

    private var gunText: String {
        switch combat.gun {
        case "reload": "CLAP TO RELOAD"
        case "arming": "RELOADING"
        default: "GUN READY"
        }
    }

    private var flashColor: Color {
        switch combat.flash {
        case .hit: TrailCanvas.green
        case .hurt: .orange
        case .down: .red
        case nil: .clear
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased()).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.title3.bold().monospacedDigit())
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Self.panel)
    }
}

/// Live camera feed behind the trail, driven by the tracker's AR session.
struct CameraView: UIViewRepresentable {
    let tracker: Tracker

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.session = tracker.session
        tracker.session.delegate = tracker
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}
