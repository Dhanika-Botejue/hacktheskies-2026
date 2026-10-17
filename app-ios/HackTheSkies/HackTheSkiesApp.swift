import SwiftUI

@main
struct HackTheSkiesApp: App {
    @AppStorage("serverURL") private var serverURL = ""

    var body: some Scene {
        WindowGroup {
            HomeView()
                // The laptop viewer shows a QR code for hacktheskies://connect?server=http://<ip>:5173.
                // Scanning it with the iPhone Camera opens the app here and remembers the laptop.
                .onOpenURL { url in
                    guard url.scheme == "hacktheskies", url.host == "connect",
                          let server = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                              .queryItems?.first(where: { $0.name == "server" })?.value
                    else { return }
                    serverURL = server
                }
                .preferredColorScheme(.dark)
        }
    }
}
