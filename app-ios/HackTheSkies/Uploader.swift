import Foundation

/// Uploads a zipped scan to the laptop (POST /api/scans), reporting progress.
final class Uploader: NSObject, ObservableObject, URLSessionTaskDelegate {
    @Published private(set) var progress: Double = 0

    /// The laptop's address as typed or from the QR code. Typed without "http://" (like the game
    /// server's "10.0.0.5:8443"), or with a stray space, URLComponents finds no host in it.
    static func laptop(_ server: String) -> URLComponents? {
        let address = server.trimmingCharacters(in: .whitespacesAndNewlines)
        let components = URLComponents(string: address.contains("://") ? address : "http://" + address)
        return components?.host?.isEmpty == false ? components : nil
    }

    func upload(zip: URL, server: String, name: String) async throws {
        guard var components = Self.laptop(server) else { throw URLError(.badURL) }
        components.path = "/api/scans"
        components.queryItems = [URLQueryItem(name: "name", value: name)]
        guard let url = components.url else { throw URLError(.badURL) }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/zip", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 600

        await MainActor.run { progress = 0 }
        let (data, response) = try await URLSession.shared.upload(for: request, fromFile: zip, delegate: self)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let message = String(data: data, encoding: .utf8) ?? ""
            throw NSError(domain: "Upload", code: (response as? HTTPURLResponse)?.statusCode ?? -1,
                          userInfo: [NSLocalizedDescriptionKey: "Laptop rejected the scan. \(message)"])
        }
        await MainActor.run { progress = 1 }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        let value = Double(totalBytesSent) / Double(totalBytesExpectedToSend)
        DispatchQueue.main.async { self.progress = value }
    }
}
