import Foundation

/// Uploads a zipped scan to the laptop (POST /api/scans), reporting progress.
final class Uploader: NSObject, ObservableObject, URLSessionTaskDelegate {
    @Published private(set) var progress: Double = 0

    func upload(zip: URL, server: String, name: String) async throws {
        guard var components = URLComponents(string: server) else { throw URLError(.badURL) }
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
