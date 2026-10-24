// Records everything this Mac is playing into a wav. `rec start call` runs
// this beside the microphone.
//
// A Core Audio process tap is the wrong listener. It delivered real samples
// for a test tone and digital silence for a playback that was clearly
// happening: meeting 2026-10-24T13-48-45 has Jacob on the microphone and
// 32 seconds of zeros on the other side, and the tap never once logged a
// sample above the noise floor. ScreenCaptureKit's audio stream is the mix
// of application audio, the same mix a screen recording gets, which is the
// audio actually leaving the Mac. It does not open the headphone microphone,
// so a Bluetooth headset is not dropped from music quality to phone quality.
//
//   YarnSystemAudio /path/meeting__call.wav
//
// The process writes `<wav>.ready` once it is listening. Samples are
// discarded until `<wav>.go` exists, which `rec` creates when the microphone
// file has started, so the two files line up. SIGINT or SIGTERM finishes the
// wav header and exits.

import CoreAudio
import CoreMedia
import Foundation
import ScreenCaptureKit

private let readySuffix = ".ready"
private let goSuffix = ".go"
private let errorSuffix = ".error"

private func permissionDenied() -> String {
    // Ventura and Sonoma call the row Screen Recording. Sequoia renamed it.
    let row: String
    if #available(macOS 15, *) {
        row = "Screen & System Audio Recording"
    } else {
        row = "Screen Recording"
    }
    return """
    Turn on Yarn in System Settings, Privacy & Security, \(row). \
    Then quit Yarn, open it again, and press Headphones.
    """
}

private let doneSuffix = ".stop"
private let finishedSuffix = ".done"

private struct CallAudioFailure: Error {
    let message: String
}

private var callAudioLogPath = ""

private func say(_ line: String) {
    fputs(line + "\n", stderr)
    fflush(stderr)
    guard !callAudioLogPath.isEmpty, let data = (line + "\n").data(using: .utf8) else {
        return
    }
    if FileManager.default.fileExists(atPath: callAudioLogPath),
       let handle = FileHandle(forWritingAtPath: callAudioLogPath) {
        handle.seekToEndOfFile()
        handle.write(data)
        try? handle.close()
    } else {
        try? data.write(to: URL(fileURLWithPath: callAudioLogPath))
    }
}

private func fail(_ wavPath: String, _ message: String) throws -> Never {
    say(message)
    try? message.write(
        toFile: wavPath + errorSuffix,
        atomically: true,
        encoding: .utf8
    )
    throw CallAudioFailure(message: message)
}

private func isPermission(_ error: Error) -> Bool {
    let ns = error as NSError
    if ns.domain.contains("ScreenCaptureKit") && ns.code == -3801 {
        return true
    }
    let lowered = String(describing: error).lowercased()
    return lowered.contains("permission") || lowered.contains("authoriz")
        || lowered.contains("tcc") || lowered.contains("not allowed")
        || lowered.contains("declined") || lowered.contains("denied")
}

private func report(_ error: Error) -> String {
    let detail = String(describing: error)
    let message = isPermission(error)
        ? permissionDenied()
        : "Yarn could not record the audio this Mac is playing. \(detail)"
    say(message)
    return message
}

private final class WavFile {
    private let handle: FileHandle
    private var dataBytes: UInt32 = 0
    let sampleRate: UInt32

    init(path: String, sampleRate: UInt32) throws {
        self.sampleRate = sampleRate
        FileManager.default.createFile(atPath: path, contents: nil)
        handle = try FileHandle(forWritingTo: URL(fileURLWithPath: path))
        try handle.write(contentsOf: Self.header(dataBytes: 0, sampleRate: sampleRate))
    }

    func append(_ samples: UnsafePointer<Int16>, count: Int) {
        guard count > 0 else { return }
        let data = Data(bytes: samples, count: count * MemoryLayout<Int16>.size)
        try? handle.write(contentsOf: data)
        dataBytes += UInt32(count * MemoryLayout<Int16>.size)
    }

    func finish() {
        do {
            try handle.seek(toOffset: 0)
            try handle.write(contentsOf: Self.header(dataBytes: dataBytes, sampleRate: sampleRate))
            try handle.close()
        } catch {
            fputs("could not finish the wav header: \(error)\n", stderr)
        }
    }

    private static func header(dataBytes: UInt32, sampleRate: UInt32) -> Data {
        var data = Data(count: 44)
        func put(_ offset: Int, _ bytes: [UInt8]) {
            data.replaceSubrange(offset..<(offset + bytes.count), with: bytes)
        }
        func le32(_ value: UInt32) -> [UInt8] {
            [
                UInt8(value & 0xff),
                UInt8((value >> 8) & 0xff),
                UInt8((value >> 16) & 0xff),
                UInt8((value >> 24) & 0xff),
            ]
        }
        func le16(_ value: UInt16) -> [UInt8] {
            [UInt8(value & 0xff), UInt8((value >> 8) & 0xff)]
        }
        put(0, Array("RIFF".utf8))
        put(4, le32(36 + dataBytes))
        put(8, Array("WAVEfmt ".utf8))
        put(16, le32(16))
        put(20, le16(1))
        put(22, le16(1))
        put(24, le32(sampleRate))
        put(28, le32(sampleRate * 2))
        put(32, le16(2))
        put(34, le16(16))
        put(36, Array("data".utf8))
        put(40, le32(dataBytes))
        return data
    }
}

private final class SampleRing {
    private let capacity = 48_000 * 12
    private let storage: UnsafeMutablePointer<Int16>
    private let lock = UnsafeMutablePointer<os_unfair_lock>.allocate(capacity: 1)
    private var readIndex = 0
    private var writeIndex = 0
    private var stored = 0

    init() {
        storage = .allocate(capacity: capacity)
        storage.initialize(repeating: 0, count: capacity)
        lock.initialize(to: os_unfair_lock())
    }

    func push(_ samples: UnsafePointer<Int16>, count: Int) {
        os_unfair_lock_lock(lock)
        let take = min(count, capacity - stored)
        var index = writeIndex
        for offset in 0..<take {
            storage[index] = samples[offset]
            index += 1
            if index == capacity { index = 0 }
        }
        writeIndex = index
        stored += take
        os_unfair_lock_unlock(lock)
    }

    func clear() {
        os_unfair_lock_lock(lock)
        readIndex = 0
        writeIndex = 0
        stored = 0
        os_unfair_lock_unlock(lock)
    }

    func pop(into destination: UnsafeMutablePointer<Int16>, max: Int) -> Int {
        os_unfair_lock_lock(lock)
        let take = min(max, stored)
        var index = readIndex
        for offset in 0..<take {
            destination[offset] = storage[index]
            index += 1
            if index == capacity { index = 0 }
        }
        readIndex = index
        stored -= take
        os_unfair_lock_unlock(lock)
        return take
    }
}

private final class Listen: NSObject, SCStreamOutput, SCStreamDelegate {
    weak var engine: Engine?

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio else { return }
        engine?.take(sampleBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        // Stopping the process also stops the stream. That is not a failure,
        // and it must not discard a wav that already has the call in it.
        guard let engine, !engine.stopFlag else { return }
        fputs("system audio stopped: \(error)\n", stderr)
        fflush(stderr)
        engine.stopFlag = true
    }
}

private final class Engine: @unchecked Sendable {
    let wavPath: String
    let ring = SampleRing()
    let listen = Listen()
    let samples = DispatchQueue(label: "com.meetingarchive.systemaudio.samples")
    let scratch: UnsafeMutablePointer<Int16>
    let scratchCapacity = 16_384
    var streams: [SCStream] = []
    var stopFlag = false
    var sampleRate: Double = 48_000
    var isFloat = true
    var bitsPerChannel = 32
    var heardSignal = false
    var loggedFormat = false
    var peak: Float = 0

    init(wavPath: String) {
        self.wavPath = wavPath
        scratch = .allocate(capacity: scratchCapacity)
        listen.engine = self
    }

    func start() async throws {
        // One attempt. Asking again in a loop is what kept the permission
        // dialog on screen after the user had already answered it.
        if stopFlag { return }
        try await openCapture()
    }

    func stopStreams() async {
        let current = streams
        streams = []
        for stream in current {
            try? await stream.stopCapture()
        }
    }

    func openCapture() async throws {
        for stream in streams {
            try? await stream.stopCapture()
        }
        streams.removeAll()
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )
        let displays = content.displays
        guard !displays.isEmpty else {
            try fail(wavPath, "Yarn could not see a display, so it cannot record the audio this Mac is playing.")
        }
        fputs("system audio displays \(displays.count)\n", stderr)
        fflush(stderr)
        for display in displays {
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            // Audio is the point. The stream still wants a frame size, and a
            // full-screen size would spend the recording copying pixels that
            // are thrown away.
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            config.showsCursor = false
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = true
            config.sampleRate = 48_000
            config.channelCount = 2
            let stream = SCStream(filter: filter, configuration: config, delegate: listen)
            try stream.addStreamOutput(listen, type: .audio, sampleHandlerQueue: samples)
            try await stream.startCapture()
            streams.append(stream)
        }
        guard !streams.isEmpty else {
            try fail(wavPath, permissionDenied())
        }
        try "ok\n".write(
            toFile: wavPath + readySuffix,
            atomically: true,
            encoding: .utf8
        )
        fputs(
            String(
                format: "system audio ready at %.0f Hz, application audio\n",
                sampleRate
            ),
            stderr
        )
        fflush(stderr)
    }

    func recordUntilStopped() throws {
        let goPath = wavPath + goSuffix
        let stopPath = wavPath + doneSuffix
        while !stopFlag && !FileManager.default.fileExists(atPath: goPath) {
            if FileManager.default.fileExists(atPath: stopPath) { stopFlag = true }
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        if stopFlag { return }
        // Samples from before the microphone file exists would put the other
        // side of the call ahead of you. Drop them and start together.
        ring.clear()
        let wav: WavFile
        do {
            wav = try WavFile(path: wavPath, sampleRate: UInt32(sampleRate.rounded()))
        } catch {
            try fail(wavPath, "Yarn could not open the video-call recording: \(error.localizedDescription)")
        }
        let output = UnsafeMutablePointer<Int16>.allocate(capacity: 4096)
        defer { output.deallocate() }
        var loggedFirst = false
        while true {
            if FileManager.default.fileExists(atPath: stopPath) { stopFlag = true }
            let count = ring.pop(into: output, max: 4096)
            if count == 0 {
                if stopFlag { break }
                RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.01))
                continue
            }
            if !loggedFirst {
                loggedFirst = true
                fputs("system audio receiving samples\n", stderr)
                fflush(stderr)
            }
            wav.append(output, count: count)
        }
        wav.finish()
        say(String(format: "system audio peak %.4f", peak))
    }

    func take(_ sampleBuffer: CMSampleBuffer) {
        noteFormat(sampleBuffer)
        let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)
        guard frameCount > 0 else { return }
        do {
            try sampleBuffer.withAudioBufferList { buffers, _ in
                guard let first = buffers.first, let data = first.mData else { return }
                let floatsInFirst = Int(first.mDataByteSize) / MemoryLayout<Float>.size
                let frames = frameCount
                if buffers.count > 1 || floatsInFirst <= frames {
                    push(frames: frames) { frame in
                        var loudest: Float = 0
                        for buffer in buffers {
                            guard let pointer = buffer.mData else { continue }
                            let value = self.number(pointer, index: frame)
                            if abs(value) > abs(loudest) { loudest = value }
                        }
                        return loudest
                    }
                    return
                }
                let channels = max(floatsInFirst / frames, 1)
                push(frames: frames) { frame in
                    var loudest: Float = 0
                    for channel in 0..<channels {
                        let value = self.number(data, index: frame * channels + channel)
                        if abs(value) > abs(loudest) { loudest = value }
                    }
                    return loudest
                }
            }
        } catch {
            fputs("system audio buffer: \(error)\n", stderr)
            fflush(stderr)
        }
    }

    private func noteFormat(_ sampleBuffer: CMSampleBuffer) {
        guard !loggedFormat,
              let description = sampleBuffer.formatDescription,
              let basic = description.audioStreamBasicDescription else {
            return
        }
        loggedFormat = true
        let format = basic
        if format.mSampleRate > 0 {
            sampleRate = format.mSampleRate
        }
        isFloat = format.mFormatFlags & kAudioFormatFlagIsFloat != 0
        if format.mBitsPerChannel > 0 {
            bitsPerChannel = Int(format.mBitsPerChannel)
        }
        fputs(
            String(
                format: "system audio format %.0f Hz, %d channel(s), %d-bit %@\n",
                format.mSampleRate,
                format.mChannelsPerFrame,
                format.mBitsPerChannel,
                isFloat ? "float" : "integer"
            ),
            stderr
        )
        fflush(stderr)
    }

    private func number(_ data: UnsafeMutableRawPointer, index: Int) -> Float {
        if isFloat {
            return data.assumingMemoryBound(to: Float.self)[index]
        }
        if bitsPerChannel <= 16 {
            let value = data.assumingMemoryBound(to: Int16.self)[index]
            return Float(value) / 32_768
        }
        let value = data.assumingMemoryBound(to: Int32.self)[index]
        return Float(value) / 2_147_483_648
    }

    private func push(frames: Int, _ sampleAt: (Int) -> Float) {
        guard frames > 0 else { return }
        var offset = 0
        while offset < frames {
            let count = min(scratchCapacity, frames - offset)
            for index in 0..<count {
                let value = sampleAt(offset + index)
                let magnitude = abs(value)
                if magnitude > peak { peak = magnitude }
                if !heardSignal && magnitude > 0.02 {
                    heardSignal = true
                    fputs("system audio heard the headphones\n", stderr)
                    fflush(stderr)
                }
                scratch[index] = Self.floatToInt16(value)
            }
            ring.push(scratch, count: count)
            offset += count
        }
    }

    private static func floatToInt16(_ value: Float) -> Int16 {
        let clamped = min(1, max(-1, value))
        return Int16(clamped * 32_767)
    }
}

// Screen-recording permission belongs to the app that calls ScreenCaptureKit.
// A helper with its own name is a second grant, and launching it again is
// what put the dialog back up. This runs inside Yarn, which is the app the
// user allows. Recording in the room never calls it.
@MainActor
final class CallAudio {
    static let shared = CallAudio()
    private var running = false
    private var engine: Engine?
    private var root = ""

    func begin(archiveRoot: String) {
        root = archiveRoot
        callAudioLogPath = archiveRoot + "/logs/call-audio.log"
    }

    /// Register Yarn in Privacy & Security and learn whether capture can start.
    ///
    /// `CGRequestScreenCaptureAccess()` is the Allow button on older macOS.
    /// On current macOS it returns without listing the app. The row appears
    /// only after this app asks ScreenCaptureKit for the audio it is playing.
    static func prepareAccess() async -> Bool {
        if CGRequestScreenCaptureAccess() {
            return true
        }
        let probe = (NSTemporaryDirectory() as NSString)
            .appendingPathComponent("yarn-permission-probe.wav")
        let engine = Engine(wavPath: probe)
        do {
            try await engine.openCapture()
            await engine.stopStreams()
            try? FileManager.default.removeItem(atPath: probe + readySuffix)
            try? FileManager.default.removeItem(atPath: probe)
            return true
        } catch {
            await engine.stopStreams()
            try? FileManager.default.removeItem(atPath: probe + errorSuffix)
            try? FileManager.default.removeItem(atPath: probe)
            return false
        }
    }

    func poll() {
        guard !root.isEmpty else { return }
        if let engine, FileManager.default.fileExists(atPath: engine.wavPath + doneSuffix) {
            engine.stopFlag = true
        }
        guard !running else { return }
        let request = root + "/.call-audio-request"
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: request),
              let modified = attrs[.modificationDate] as? Date,
              Date().timeIntervalSince(modified) < 30,
              let text = try? String(contentsOfFile: request, encoding: .utf8),
              let wav = text.split(whereSeparator: \.isNewline).first.map(String.init),
              wav.hasSuffix(".wav") else { return }
        if FileManager.default.fileExists(atPath: wav + doneSuffix)
            || FileManager.default.fileExists(atPath: wav + ".ready")
            || FileManager.default.fileExists(atPath: wav + ".error")
            || FileManager.default.fileExists(atPath: wav + finishedSuffix) {
            return
        }
        running = true
        let recorder = Engine(wavPath: wav)
        engine = recorder
        say("system audio starting \(wav)")
        Task.detached {
            var failed: Error?
            do {
                try await recorder.start()
                try recorder.recordUntilStopped()
            } catch {
                failed = error
            }
            await recorder.stopStreams()
            if let failed, !FileManager.default.fileExists(atPath: wav + ".error") {
                let message = report(failed)
                try? message.write(toFile: wav + ".error", atomically: true, encoding: .utf8)
            }
            try? "done\n".write(toFile: wav + finishedSuffix, atomically: true, encoding: .utf8)
            await MainActor.run {
                CallAudio.shared.engine = nil
                CallAudio.shared.running = false
            }
        }
    }
}
