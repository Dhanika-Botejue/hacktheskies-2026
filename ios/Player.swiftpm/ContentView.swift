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
    @State private var follow = true
    @State private var joined = false
    @AppStorage("server") private var server = "10.110.0.189:8443"
    @AppStorage("name") private var name = ""

    private static let panel = Color(red: 0.11, green: 0.12, blue: 0.15)

    var body: some View {
        ZStack {
            CameraView(tracker: tracker).ignoresSafeArea()

            VStack(spacing: 0) {
                gameBar
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
        .onAppear {
            tracker.onPose = { [client] x, z, yaw, tracking, epoch, time in
                client.cameraPose(x: x, z: z, yaw: yaw, tracking: tracking, epoch: epoch, time: time)
            }
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
                        joined = false
                    }
                    .font(.footnote)
                }
            } else {
                HStack {
                    TextField("Your name", text: $name).textFieldStyle(.roundedBorder)
                    TextField("Server", text: $server)
                        .textFieldStyle(.roundedBorder)
                        .keyboardType(.numbersAndPunctuation)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                    Button("Join") {
                        client.connect(server: server, name: name.isEmpty ? "iPhone" : name)
                        joined = true
                    }
                    .buttonStyle(.borderedProminent)
                }
                if let error = client.error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
            }
        }
        .padding(10)
        .background(Self.panel)
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
