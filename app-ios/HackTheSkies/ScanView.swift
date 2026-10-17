import ARKit
import SceneKit
import SwiftUI

/// Scan Map screen: camera view while walking around, then automatic packaging + upload.
struct ScanView: View {
    let serverURL: String
    @Environment(\.dismiss) private var dismiss
    @StateObject private var scan = ScanSession()
    @StateObject private var uploader = Uploader()
    @State private var phase: Phase = .scanning
    @State private var zip: URL?

    enum Phase: Equatable {
        case scanning, packaging, uploading, done, failed(String)
    }

    var body: some View {
        ZStack {
            ARCameraView(session: scan.session).ignoresSafeArea()

            VStack {
                topBar
                Spacer()
                if let hint = scan.hint, phase == .scanning {
                    Text(hint)
                        .font(.headline)
                        .padding(.horizontal, 16).padding(.vertical, 8)
                        .background(.orange.opacity(0.9), in: Capsule())
                }
                Spacer()
                bottomPanel
            }
            .padding()
        }
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
            do { try scan.start() } catch { phase = .failed(error.localizedDescription) }
        }
        .onDisappear { UIApplication.shared.isIdleTimerDisabled = false }
    }

    private var topBar: some View {
        HStack {
            Button("Cancel") {
                scan.cancel()
                dismiss()
            }
            .buttonStyle(.bordered)
            .disabled(phase == .packaging || phase == .uploading)
            Spacer()
            VStack(alignment: .trailing) {
                Text("\(scan.keyframes) photos").font(.headline.monospacedDigit())
                if scan.hasLiDAR { Text("\(scan.points) depth points").font(.caption.monospacedDigit()) }
            }
            .padding(8)
            .background(.black.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    @ViewBuilder private var bottomPanel: some View {
        VStack(spacing: 12) {
            switch phase {
            case .scanning:
                Text("Walk slowly through the play area. Sweep each wall high and low, then come back to where you started.")
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                Button {
                    Task { await finishAndUpload() }
                } label: {
                    Text("Done").font(.title3.bold()).frame(maxWidth: .infinity, minHeight: 50)
                }
                .buttonStyle(.borderedProminent)
                .disabled(scan.keyframes < 10)
            case .packaging:
                ProgressView("Packaging scan…")
            case .uploading:
                ProgressView("Uploading to laptop…", value: uploader.progress)
            case .done:
                Label("Uploaded! The map is training on the laptop.", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Button("Back") { dismiss() }.buttonStyle(.borderedProminent)
            case .failed(let message):
                Text(message).foregroundStyle(.red).multilineTextAlignment(.center)
                HStack {
                    if zip != nil {
                        Button("Retry upload") { Task { await upload() } }.buttonStyle(.borderedProminent)
                    }
                    Button("Close") { dismiss() }.buttonStyle(.bordered)
                }
            }
        }
        .padding()
        .frame(maxWidth: .infinity)
        .background(.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 14))
    }

    private func finishAndUpload() async {
        phase = .packaging
        do {
            let dir = try await scan.finish()
            zip = try await Task.detached { try zipDirectory(dir) }.value
            try? FileManager.default.removeItem(at: dir)
            await upload()
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    private func upload() async {
        guard let zip else { return }
        phase = .uploading
        let name = "Scan " + Date().formatted(date: .omitted, time: .shortened)
        do {
            try await uploader.upload(zip: zip, server: serverURL, name: name)
            try? FileManager.default.removeItem(at: zip)
            self.zip = nil
            phase = .done
        } catch {
            phase = .failed("Upload failed: \(error.localizedDescription)")
        }
    }
}

/// Live camera feed with ARKit's tracked feature points drawn on top.
struct ARCameraView: UIViewRepresentable {
    let session: ARSession

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView()
        view.session = session
        view.debugOptions = [.showFeaturePoints]
        view.automaticallyUpdatesLighting = false
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}
