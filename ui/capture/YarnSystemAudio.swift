// Records what this Mac is playing into a wav, while headphones stay the
// output device. A video call's far side never reaches BlackHole when the
// headphones are the only output, so `rec start call` runs this beside the
// microphone capture.
//
//   YarnSystemAudio /path/meeting__call.wav
//
// The process starts the tap immediately and writes `<wav>.ready` once macOS
// has allowed it. Samples are discarded until `<wav>.go` exists, which `rec`
// creates when the microphone file has started, so the two files line up.
// SIGINT or SIGTERM finishes the wav header and exits.

import AudioToolbox
import CoreAudio
import Foundation

private let readySuffix = ".ready"
private let goSuffix = ".go"
private let errorSuffix = ".error"

private func fourChar(_ status: OSStatus) -> String {
    let value = UInt32(bitPattern: status)
    let bytes = [
        UInt8((value >> 24) & 0xff),
        UInt8((value >> 16) & 0xff),
        UInt8((value >> 8) & 0xff),
        UInt8(value & 0xff),
    ]
    if bytes.allSatisfy({ $0 >= 32 && $0 < 127 }) {
        return String(bytes: bytes, encoding: .utf8) ?? "\(status)"
    }
    return "\(status)"
}

private func permissionMessage(_ status: OSStatus) -> String {
    """
    Yarn could not hear the video call (\(fourChar(status))). macOS keeps that \
    audio in your headphones unless Yarn is allowed to record it. Open System \
    Settings, go to Privacy & Security, then Screen & System Audio Recording, \
    and turn on Yarn System Audio. Then start the video call recording again.
    """
}

private func fail(_ wavPath: String, _ message: String) -> Never {
    fputs(message + "\n", stderr)
    fflush(stderr)
    try? message.write(
        toFile: wavPath + errorSuffix,
        atomically: true,
        encoding: .utf8
    )
    exit(1)
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

private final class Engine {
    let wavPath: String
    let ring = SampleRing()
    let scratch: UnsafeMutablePointer<Int16>
    let scratchCapacity = 16_384
    var stopFlag = false
    var tapID = AudioObjectID(kAudioObjectUnknown)
    var aggregateID = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?
    var sampleRate: Double = 48_000
    var channelCount: Int = 0
    var bytesPerFrame: Int = 0
    var isFloat = true
    var isNonInterleaved = false
    var bitsPerChannel = 32

    init(wavPath: String) {
        self.wavPath = wavPath
        scratch = .allocate(capacity: scratchCapacity)
    }

    func prepare() throws {
        var excluded: [AudioObjectID] = []
        if let objectID = Self.processObjectID(pid: getpid()),
           objectID != AudioObjectID(kAudioObjectUnknown) {
            excluded = [objectID]
        }
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
        description.name = "Yarn System Audio"
        description.uuid = UUID()
        description.isPrivate = true
        description.muteBehavior = .unmuted

        var created = AudioObjectID(kAudioObjectUnknown)
        let tapStatus = AudioHardwareCreateProcessTap(description, &created)
        guard tapStatus == noErr, created != AudioObjectID(kAudioObjectUnknown) else {
            fail(wavPath, permissionMessage(tapStatus))
        }
        tapID = created

        let uid = Self.tapUID(tapID) ?? description.uuid.uuidString
        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Yarn System Audio",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: NSNumber(value: 1),
            kAudioAggregateDeviceIsStackedKey: NSNumber(value: 0),
            kAudioAggregateDeviceTapAutoStartKey: NSNumber(value: 0),
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapUIDKey: uid,
                    kAudioSubTapDriftCompensationKey: NSNumber(value: 1),
                ],
            ],
        ]
        var aggregateDevice = AudioObjectID(kAudioObjectUnknown)
        let aggregateStatus = AudioHardwareCreateAggregateDevice(
            aggregate as CFDictionary, &aggregateDevice
        )
        guard aggregateStatus == noErr else {
            fail(wavPath, permissionMessage(aggregateStatus))
        }
        aggregateID = aggregateDevice
        readFormat()

        var proc: AudioDeviceIOProcID?
        let procStatus = AudioDeviceCreateIOProcIDWithBlock(&proc, aggregateID, nil) { _, inputData, _, _, _ in
            engine?.consume(inputData)
        }
        guard procStatus == noErr, let proc else {
            fail(wavPath, "Yarn could not listen to the video call (\(fourChar(procStatus))).")
        }
        procID = proc
        let startStatus = AudioDeviceStart(aggregateID, proc)
        guard startStatus == noErr else {
            fail(wavPath, "Yarn could not start listening to the video call (\(fourChar(startStatus))).")
        }
        try "ok\n".write(
            toFile: wavPath + readySuffix,
            atomically: true,
            encoding: .utf8
        )
        fputs(
            String(
                format: "system audio ready at %.0f Hz, %d channel(s)\n",
                sampleRate,
                channelCount
            ),
            stderr
        )
        fflush(stderr)
    }

    func recordUntilStopped() {
        let goPath = wavPath + goSuffix
        while !stopFlag && !FileManager.default.fileExists(atPath: goPath) {
            usleep(20_000)
        }
        if stopFlag {
            shutdown(writeWav: false)
            return
        }
        // Samples from before the microphone file exists would put the other
        // side of the call ahead of you. Drop them and start together.
        ring.clear()
        let wav: WavFile
        do {
            wav = try WavFile(path: wavPath, sampleRate: UInt32(sampleRate.rounded()))
        } catch {
            fail(wavPath, "Yarn could not open the video-call recording: \(error.localizedDescription)")
        }
        let output = UnsafeMutablePointer<Int16>.allocate(capacity: 4096)
        defer { output.deallocate() }
        var loggedFirst = false
        while true {
            let count = ring.pop(into: output, max: 4096)
            if count == 0 {
                if stopFlag { break }
                usleep(10_000)
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
        shutdown(writeWav: true)
    }

    func consume(_ listPointer: UnsafePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: listPointer)
        )
        guard channelCount > 0, let first = buffers.first, first.mData != nil else { return }
        if isNonInterleaved {
            let width = max(bitsPerChannel / 8, 1)
            let frames = Int(first.mDataByteSize) / width
            push(frames: frames) { frame in
                var sum: Float = 0
                var heard = 0
                for buffer in buffers {
                    guard let pointer = buffer.mData else { continue }
                    sum += self.sample(pointer, index: frame)
                    heard += 1
                }
                return heard > 0 ? sum / Float(heard) : 0
            }
            return
        }
        guard let data = first.mData else { return }
        let frameBytes = max(bytesPerFrame, channelCount * max(bitsPerChannel / 8, 1))
        guard frameBytes > 0 else { return }
        let frames = Int(first.mDataByteSize) / frameBytes
        push(frames: frames) { frame in
            var sum: Float = 0
            for channel in 0..<self.channelCount {
                sum += self.sample(data, index: frame * self.channelCount + channel)
            }
            return sum / Float(max(self.channelCount, 1))
        }
    }

    private func push(frames: Int, _ sampleAt: (Int) -> Float) {
        guard frames > 0 else { return }
        var offset = 0
        while offset < frames {
            let count = min(scratchCapacity, frames - offset)
            for index in 0..<count {
                scratch[index] = Self.floatToInt16(sampleAt(offset + index))
            }
            ring.push(scratch, count: count)
            offset += count
        }
    }

    private func shutdown(writeWav: Bool) {
        if let procID {
            AudioDeviceStop(aggregateID, procID)
            AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        if aggregateID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyProcessTap(tapID)
        }
        _ = writeWav
        exit(0)
    }

    private func readFormat() {
        var format = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamFormat,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var status = AudioObjectGetPropertyData(aggregateID, &address, 0, nil, &size, &format)
        if status != noErr || format.mChannelsPerFrame == 0 {
            address.mScope = kAudioObjectPropertyScopeOutput
            size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
            status = AudioObjectGetPropertyData(aggregateID, &address, 0, nil, &size, &format)
        }
        if status != noErr || format.mSampleRate == 0 {
            address = AudioObjectPropertyAddress(
                mSelector: kAudioTapPropertyFormat,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
            status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &format)
        }
        guard status == noErr, format.mChannelsPerFrame > 0 else {
            fail(wavPath, "Yarn could not read the video-call audio format (\(fourChar(status))).")
        }
        sampleRate = format.mSampleRate > 0 ? format.mSampleRate : 48_000
        channelCount = Int(format.mChannelsPerFrame)
        bytesPerFrame = Int(format.mBytesPerFrame)
        bitsPerChannel = Int(format.mBitsPerChannel == 0 ? 32 : format.mBitsPerChannel)
        isFloat = format.mFormatFlags & kAudioFormatFlagIsFloat != 0
        isNonInterleaved = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
    }

    private func sample(_ data: UnsafeMutableRawPointer, index: Int) -> Float {
        if isFloat {
            return data.assumingMemoryBound(to: Float.self)[index]
        }
        if bitsPerChannel <= 16 {
            let value = data.assumingMemoryBound(to: Int16.self)[index]
            return Float(value) / 32768
        }
        let value = data.assumingMemoryBound(to: Int32.self)[index]
        return Float(value) / 2_147_483_648
    }

    private static func floatToInt16(_ value: Float) -> Int16 {
        let clamped = min(1, max(-1, value))
        return Int16(clamped * 32_767)
    }

    private static func processObjectID(pid: pid_t) -> AudioObjectID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var pid = pid
        var objectID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            UInt32(MemoryLayout<pid_t>.size),
            &pid,
            &size,
            &objectID
        )
        guard status == noErr else { return nil }
        return objectID
    }

    private static func tapUID(_ tapID: AudioObjectID) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var unmanaged: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = withUnsafeMutablePointer(to: &unmanaged) { pointer in
            AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, pointer)
        }
        guard status == noErr, let unmanaged else { return nil }
        return unmanaged.takeRetainedValue() as String
    }
}

// The IO callback only needs the engine, and the signal source lives for the
// whole process. A global keeps both reachable without a run-loop owner.
private var engine: Engine?

private func requestStop() {
    engine?.stopFlag = true
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    fputs("usage: YarnSystemAudio <wav>\n", stderr)
    exit(2)
}

private let recorder = Engine(wavPath: arguments[1])
engine = recorder

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
interrupt.setEventHandler { requestStop() }
interrupt.resume()
let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
terminate.setEventHandler { requestStop() }
terminate.resume()

do {
    try recorder.prepare()
} catch {
    fail(arguments[1], "Yarn could not record the video call: \(error.localizedDescription)")
}

recorder.recordUntilStopped()
