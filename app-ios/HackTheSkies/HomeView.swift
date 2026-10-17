import ARKit
import SwiftUI

struct HomeView: View {
    @AppStorage("serverURL") private var serverURL = ""
    @State private var reachable: Bool?
    @State private var scanning = false
    @State private var editingServer = false

    var body: some View {
        VStack(spacing: 28) {
            Spacer()
            VStack(spacing: 6) {
                Text("HackTheSkies").font(.largeTitle.bold())
                Text("Capture the flag in the real world").foregroundStyle(.secondary)
            }

            Button {
                scanning = true
            } label: {
                Label("Scan Map", systemImage: "camera.viewfinder")
                    .font(.title2.bold())
                    .frame(maxWidth: .infinity, minHeight: 64)
            }
            .buttonStyle(.borderedProminent)
            .disabled(serverURL.isEmpty || !ARWorldTrackingConfiguration.isSupported)
            .padding(.horizontal, 32)

            serverStatus
            Spacer()
        }
        .padding()
        .task(id: serverURL) { await ping() }
        .fullScreenCover(isPresented: $scanning) {
            ScanView(serverURL: serverURL)
        }
        .alert("Laptop address", isPresented: $editingServer) {
            TextField("http://192.168.1.20:5173", text: $serverURL)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Button("OK") {}
        } message: {
            Text("Shown under the QR code in the viewer.")
        }
    }

    @ViewBuilder private var serverStatus: some View {
        VStack(spacing: 8) {
            if serverURL.isEmpty {
                Text("Not connected to a laptop.\nScan the QR code in the viewer with the iPhone Camera.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            } else {
                HStack(spacing: 8) {
                    Circle()
                        .fill(reachable == true ? Color.green : reachable == false ? Color.red : Color.gray)
                        .frame(width: 10, height: 10)
                    Text(serverURL).font(.footnote.monospaced()).foregroundStyle(.secondary)
                }
                if reachable == false {
                    Text("Can't reach the laptop. Same Wi-Fi? Is the viewer running?")
                        .font(.footnote).foregroundStyle(.red)
                }
            }
            Button(serverURL.isEmpty ? "Enter address manually" : "Change address") { editingServer = true }
                .font(.footnote)
        }
        .padding(.horizontal)
    }

    private func ping() async {
        guard let url = URL(string: serverURL)?.appendingPathComponent("api/maps") else {
            reachable = nil
            return
        }
        reachable = nil
        var request = URLRequest(url: url)
        request.timeoutInterval = 4
        let ok = (try? await URLSession.shared.data(for: request)).map { ($0.1 as? HTTPURLResponse)?.statusCode == 200 }
        reachable = ok ?? false
    }
}
