// Reflect's native Metal window and simulated OpenXR head/controllers.
#import <Cocoa/Cocoa.h>
#import <MetalKit/MetalKit.h>
#import <simd/simd.h>
#include "image_frame.h"
#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
#include <signal.h>
#include <atomic>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <mutex>
#include <map>
#include <set>
#include <string>
#include <thread>
#include <vector>

namespace proto = refract::protocol;
using Clock = std::chrono::steady_clock;
static std::mutex inputMutex, frameMutex;
static proto::PoseFrame latestPose;
struct Pixels { proto::ImageFrameHeader header; proto::ImageProjection projection; proto::CompositeHeader composite; std::vector<proto::CompositeQuad> quads; std::vector<uint8_t> data; };
static Pixels pending;
static bool hasPending = false;
static std::atomic<uint64_t> received{0};
static int eyeWidth = 1024, eyeHeight = 1024, refreshRate = 90;
static uint64_t quitAfter = 0;
static std::string capturePath, readyPath;

static uint64_t now_ns() { return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count(); }
static bool read_all(int fd, void* buffer, size_t size) {
    auto* p = static_cast<uint8_t*>(buffer);
    while (size) { ssize_t n = recv(fd, p, size, 0); if (n <= 0) return false; p += n; size -= n; }
    return true;
}
static bool write_all(int fd, const void* buffer, size_t size) {
    auto* p = static_cast<const uint8_t*>(buffer);
    while (size) { ssize_t n = send(fd, p, size, 0); if (n <= 0) return false; p += n; size -= n; }
    return true;
}
static int listener(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0), on = 1;
    if (fd < 0) { perror("socket"); exit(1); }
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof(on));
    sockaddr_in address{}; address.sin_family = AF_INET; address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)) || listen(fd, 4)) {
        fprintf(stderr, "Reflect: cannot listen on localhost:%d: ", port); perror("bind/listen"); exit(1);
    }
    return fd;
}
static void poses(int server) {
    for (;;) {
        int fd = accept(server, nullptr, nullptr);
        if (fd < 0) continue;
        std::thread([fd] {
            int on = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &on, sizeof(on));
            timeval timeout{2, 0}; setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
            for (;;) {
                proto::PoseFrame frame;
                { std::lock_guard lock(inputMutex); frame = latestPose; }
                frame.monotonic_time_ns = now_ns();
                if (!write_all(fd, &frame, sizeof(frame))) break;
                std::this_thread::sleep_for(std::chrono::microseconds(1000000 / refreshRate));
            }
            close(fd);
        }).detach();
    }
}
static void images(int server) {
    for (;;) {
        int fd = accept(server, nullptr, nullptr);
        if (fd < 0) continue;
        timeval timeout{15, 0}; setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        for (;;) {
            Pixels pixels;
            auto& h = pixels.header;
            if (!read_all(fd, &h, sizeof(h))) break;
            const bool atlas = h.version == proto::kCompositePixelFrameVersion;
            const bool projected = h.version == 2 || h.version == 4 || atlas;
            const uint32_t extra = atlas ? h.header_size - 160 : 0;
            uint64_t size = uint64_t(h.width) * h.height * h.layers * 4;
            if (h.magic != proto::kImageFrameMagic || (h.version != 1 && !projected) || h.type != 2 ||
                h.format != 1 || h.bytes_per_pixel != 4 || h.reserved || !h.width || !h.height ||
                h.width > 8192 || h.height > 8192 || !h.layers || h.layers > 2 ||
                (projected && h.layers != 2) || (!atlas && h.header_size != 64 + (projected ? 96 : 0)) ||
                (atlas && (h.header_size < 176 || extra > sizeof(proto::CompositeHeader) + proto::kMaxCompositeQuads * sizeof(proto::CompositeQuad))) ||
                h.payload_size != size || size > 128ull * 1024 * 1024) {
                fprintf(stderr, "Reflect: unsupported or invalid frame (version %u); use pixel transport.\n", h.version); break;
            }
            if (projected && (!read_all(fd, &pixels.projection, sizeof(pixels.projection)) ||
                !(h.version == 4 ? proto::valid_quads(pixels.projection) : proto::valid_projection(pixels.projection)))) break;
            if (atlas) {
                std::vector<uint8_t> table(extra);
                if (!read_all(fd, table.data(), table.size()) || !proto::valid_composite(table.data(), table.size(), h.width, h.height)) break;
                memcpy(&pixels.composite, table.data(), sizeof(pixels.composite));
                pixels.quads.resize(pixels.composite.quad_count);
                if (!pixels.quads.empty()) memcpy(pixels.quads.data(), table.data() + sizeof(pixels.composite), pixels.quads.size() * sizeof(proto::CompositeQuad));
            }
            pixels.data.resize(size);
            if (!read_all(fd, pixels.data.data(), size)) break;
            { std::lock_guard lock(frameMutex); pending = std::move(pixels); hasPending = true; }
            if (++received == 1) fprintf(stderr, "Reflect: receiving Android frames (%ux%u).\n", h.width, h.height);
        }
        close(fd);
    }
}

static simd_quatf rotation(float yaw, float pitch) {
    return simd_mul(simd_quaternion(yaw, simd_float3{0,1,0}), simd_quaternion(pitch, simd_float3{1,0,0}));
}
static proto::Pose pose(simd_float3 position, simd_quatf q) {
    return {position.x,position.y,position.z,q.vector.x,q.vector.y,q.vector.z,q.vector.w};
}
static simd_quatf quaternion(const proto::Pose& p) { return simd_quaternion(simd_float4{p.qx,p.qy,p.qz,p.qw}); }
struct Vertex { simd_float4 position; simd_float2 uv; simd_float2 pad{}; };

@interface ReflectView : MTKView <MTKViewDelegate> {
    id<MTLCommandQueue> queue;
    id<MTLRenderPipelineState> pipeline;
    id<MTLRenderPipelineState> panelPipeline;
    id<MTLTexture> eyes[2];
    proto::ImageProjection composition;
    bool panelFrame;
    bool compositeFrame;
    proto::CompositeHeader composite;
    std::vector<proto::CompositeQuad> compositeQuads;
    std::set<unsigned short> keys;
    std::map<unsigned short,uint64_t> keyPulse;
    float yaw, pitch, reach;
    simd_float3 position;
    bool mouseLocked, trigger, grip, stereo;
    uint64_t triggerPulse, gripPulse;
    NSPoint lastPointer;
    uint64_t sequence, displayed;
    Clock::time_point lastTick;
    Clock::time_point fpsSampleTime;
    uint64_t fpsSampleFrames;
    uint32_t frameWidth, frameHeight;
    double measuredFPS;
}
- (void)tick;
- (void)releaseMouse;
@end

@implementation ReflectView
- (instancetype)initWithFrame:(NSRect)frame device:(id<MTLDevice>)device {
    self = [super initWithFrame:frame device:device];
    if (!self) return nil;
    self.colorPixelFormat = MTLPixelFormatBGRA8Unorm;
    self.clearColor = MTLClearColorMake(.015,.015,.02,1);
    self.preferredFramesPerSecond = refreshRate;
    // Drive rendering from the same main-thread timer as input. MTKView's display
    // link can stop delivering callbacks when a CLI-launched window is occluded.
    self.delegate = self; self.paused = YES; self.enableSetNeedsDisplay = NO;
    reach = .4f; position = {0,1.65f,0}; lastTick = Clock::now();
    queue = [device newCommandQueue];
    NSString* shader = @"#include <metal_stdlib>\nusing namespace metal;\n"
      "struct V { float4 p; float2 uv; float2 pad; }; struct O { float4 p [[position]]; float2 uv; };\n"
      "vertex O vs(uint i [[vertex_id]], constant V* v [[buffer(0)]]) { return {v[i].p,v[i].uv}; }\n"
      "fragment float4 fs(O v [[stage_in]], texture2d<float> t [[texture(0)]]) { constexpr sampler s(filter::linear,address::clamp_to_edge); return t.sample(s,v.uv); }";
    NSError* error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:shader options:nil error:&error];
    if (!library) { fprintf(stderr, "Reflect Metal: %s\n", error.description.UTF8String); exit(1); }
    MTLRenderPipelineDescriptor* descriptor = [MTLRenderPipelineDescriptor new];
    descriptor.vertexFunction = [library newFunctionWithName:@"vs"];
    descriptor.fragmentFunction = [library newFunctionWithName:@"fs"];
    descriptor.colorAttachments[0].pixelFormat = self.colorPixelFormat;
    pipeline = [device newRenderPipelineStateWithDescriptor:descriptor error:&error];
    descriptor.colorAttachments[0].blendingEnabled = YES;
    descriptor.colorAttachments[0].sourceRGBBlendFactor = MTLBlendFactorSourceAlpha;
    descriptor.colorAttachments[0].destinationRGBBlendFactor = MTLBlendFactorOneMinusSourceAlpha;
    panelPipeline = [device newRenderPipelineStateWithDescriptor:descriptor error:&error];
    if (!pipeline || !panelPipeline) { fprintf(stderr, "Reflect Metal: %s\n", error.description.UTF8String); exit(1); }
    [NSTimer scheduledTimerWithTimeInterval:1.0/refreshRate repeats:YES block:^(NSTimer*) { [self tick]; }];
    return self;
}
- (BOOL)acceptsFirstResponder { return YES; }
- (BOOL)acceptsFirstMouse:(NSEvent*)event { (void)event; return YES; }
- (void)keyDown:(NSEvent*)event {
    if (event.keyCode == 53) { [self releaseMouse]; return; }
    if (!event.isARepeat && event.keyCode == 48) { stereo = !stereo; return; }
    keys.insert(event.keyCode);
    // Preserve brief presses until Android has had several pose records to observe them.
    keyPulse[event.keyCode] = now_ns() + 50000000;
}
- (void)keyUp:(NSEvent*)event { keys.erase(event.keyCode); }
- (void)flagsChanged:(NSEvent*)event {
    if (event.modifierFlags & NSEventModifierFlagShift) keys.insert(56); else keys.erase(56);
}
- (void)mouseDown:(NSEvent*)event {
    (void)event;
    if (!mouseLocked) {
        mouseLocked = true; [NSCursor hide]; CGAssociateMouseAndMouseCursorPosition(false);
        lastPointer = event.locationInWindow;
    } else { trigger = true; triggerPulse = now_ns() + 50000000; }
}
- (void)mouseUp:(NSEvent*)event { (void)event; trigger = false; }
- (void)rightMouseDown:(NSEvent*)event { (void)event; grip = true; gripPulse = now_ns() + 50000000; }
- (void)rightMouseUp:(NSEvent*)event { (void)event; grip = false; }
- (void)mouseMoved:(NSEvent*)event {
    if (!mouseLocked) return;
    float dx = event.deltaX, dy = event.deltaY;
    NSPoint pointer = event.locationInWindow;
    // Some pointing devices send absolute motion with zero deltas.
    if (!dx && !dy) { dx = pointer.x-lastPointer.x; dy = lastPointer.y-pointer.y; }
    lastPointer = pointer;
    yaw -= dx * .003f;
    pitch = std::clamp(pitch - dy * .003f, -1.396f, 1.396f);
}
- (void)mouseDragged:(NSEvent*)event { [self mouseMoved:event]; }
- (void)rightMouseDragged:(NSEvent*)event { [self mouseMoved:event]; }
- (void)scrollWheel:(NSEvent*)event { reach = std::clamp(reach + float(event.scrollingDeltaY)*.01f,.1f,1.5f); }
- (void)releaseMouse {
    if (mouseLocked) { [NSCursor unhide]; CGAssociateMouseAndMouseCursorPosition(true); mouseLocked = false; }
    trigger = grip = false; keys.clear(); keyPulse.clear(); triggerPulse = gripPulse = 0;
}
- (void)tick {
    float dt = std::min(.05, std::chrono::duration<double>(Clock::now()-lastTick).count()); lastTick = Clock::now();
    if (displayed) {
        const double elapsed = std::chrono::duration<double>(lastTick-fpsSampleTime).count();
        if (elapsed >= .5) {
            measuredFPS = (displayed-fpsSampleFrames)/elapsed;
            fpsSampleFrames = displayed;
            fpsSampleTime = lastTick;
            self.window.title = [NSString stringWithFormat:@"Reflect — %ux%u — %.1f FPS — click to capture mouse; Esc releases",frameWidth,frameHeight,measuredFPS];
        }
    }
    const uint64_t timestamp = now_ns();
    auto down = [&](int key) { auto pulse = keyPulse.find(key); return keys.count(key) || (pulse != keyPulse.end() && pulse->second > timestamp); };
    if (down(115)) { position = {0,1.65f,0}; yaw = pitch = 0; }
    float x = float(down(2)) - down(0), z = float(down(1)) - down(13); // D A S W
    float length = std::hypot(x,z);
    if (length) position += simd_act(rotation(yaw,0), simd_float3{x/length,0,z/length}) * dt * (down(56)?3.f:1.5f);
    auto q = rotation(yaw, down(17)?0:pitch); // T: calibration pose
    proto::PoseFrame frame;
    frame.sequence = sequence++;
    frame.monotonic_time_ns = now_ns();
    frame.hmd = pose(position,q);
    frame.hmd_flags = frame.local_origin_flags = 15;
    frame.render_width = eyeWidth; frame.render_height = eyeHeight;
    frame.display_period_ns = 1000000000 / refreshRate;
    for (int i=0;i<2;i++) {
        simd_float3 local = {i? .22f:-.22f,-.28f,-reach};
        if (down(17)) local = {i?.8f:-.8f,-.22f,0};
        if (i && down(8)) local = {0,0,-reach}; // C: reach at view centre
        frame.aim[i] = pose(position + simd_act(q,local),q);
        frame.grip_flags[i] = frame.aim_flags[i] = 15; frame.aim_active[i] = 1;
        auto& hand = frame.controllers[i]; hand.active = 1;
        if (down(i?49:6) || (i && down(36))) hand.buttons |= proto::PrimaryClick | proto::PrimaryTouch;
        if (down(i?51:7)) hand.buttons |= proto::SecondaryClick | proto::SecondaryTouch;
        if (!i && down(46)) hand.buttons |= proto::MenuClick;
        hand.trigger = down(i?14:12) || (i && (trigger || triggerPulse > timestamp)); // E Q
        hand.squeeze = down(i?15:3) || (i && (grip || gripPulse > timestamp)); // R F
        if (hand.trigger) hand.buttons |= proto::TriggerTouch;
        if (i) { hand.stick_x = float(down(124))-down(123); hand.stick_y = float(down(126))-down(125); }
        if (hand.stick_x || hand.stick_y) hand.buttons |= proto::StickTouch;
    }
    frame.left_controller = frame.aim[0]; frame.right_controller = frame.aim[1];
    { std::lock_guard lock(inputMutex); latestPose = frame; }
    [self draw];
}
- (void)mtkView:(MTKView*)view drawableSizeWillChange:(CGSize)size { (void)view; (void)size; }
- (void)drawInMTKView:(MTKView*)view {
    (void)view;
    Pixels pixels; bool update = false;
    { std::lock_guard lock(frameMutex); if (hasPending) { pixels = std::move(pending); hasPending = false; update = true; } }
    if (update) {
        auto& h = pixels.header;
        composition = pixels.projection; panelFrame = h.version == 4;
        compositeFrame = h.version == proto::kCompositePixelFrameVersion;
        composite = pixels.composite; compositeQuads = std::move(pixels.quads);
        for (uint32_t i=0;i<h.layers;i++) {
            if (!eyes[i] || eyes[i].width != h.width || eyes[i].height != h.height) {
                MTLTextureDescriptor* d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:h.width height:h.height mipmapped:NO];
                d.storageMode = MTLStorageModeShared; d.usage = MTLTextureUsageShaderRead;
                eyes[i] = [self.device newTextureWithDescriptor:d];
            }
            [eyes[i] replaceRegion:MTLRegionMake2D(0,0,h.width,h.height) mipmapLevel:0 withBytes:pixels.data.data()+uint64_t(i)*h.width*h.height*4 bytesPerRow:h.width*4];
        }
        if (h.layers == 1) eyes[1] = eyes[0];
        const uint32_t shownWidth = compositeFrame && composite.scene_width ? composite.scene_width : h.width;
        const uint32_t shownHeight = compositeFrame && composite.scene_height ? composite.scene_height : h.height;
        const bool resolutionChanged = frameWidth != shownWidth || frameHeight != shownHeight;
        frameWidth = shownWidth; frameHeight = shownHeight;
        displayed++;
        if (displayed == 1) {
            fpsSampleTime = Clock::now();
            fpsSampleFrames = displayed;
        }
        if (displayed == 1 && !capturePath.empty()) {
            NSBitmapImageRep* bitmap = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:nullptr pixelsWide:h.width pixelsHigh:h.height bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:h.width*4 bitsPerPixel:32];
            memcpy(bitmap.bitmapData,pixels.data.data(),uint64_t(h.width)*h.height*4);
            [[bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:[NSString stringWithUTF8String:capturePath.c_str()] atomically:YES];
        }
        if (resolutionChanged) self.window.title = [NSString stringWithFormat:@"Reflect — %ux%u — %.1f FPS — click to capture mouse; Esc releases",frameWidth,frameHeight,measuredFPS];
    }
    id<CAMetalDrawable> drawable = self.currentDrawable;
    MTLRenderPassDescriptor* pass = self.currentRenderPassDescriptor;
    if (!drawable || !pass) return;
    id<MTLCommandBuffer> command = [queue commandBuffer];
    id<MTLRenderCommandEncoder> encoder = [command renderCommandEncoderWithDescriptor:pass];
    [encoder setRenderPipelineState:panelFrame?panelPipeline:pipeline];
    int count = stereo?2:1;
    for (int eye=0;eye<count;eye++) {
        if (!eyes[eye]) continue;
        double w = self.drawableSize.width/count, h = self.drawableSize.height;
        [encoder setViewport:MTLViewport{eye*w,0,w,h,0,1}];
        const bool hasScene = !compositeFrame || composite.scene_width != 0;
        uint32_t layers = compositeFrame ? uint32_t(compositeQuads.size()) + hasScene : panelFrame?composition.quad_count():1;
        for (uint32_t layer=0;layer<layers;layer++) {
            Vertex vertices[4] = {{{-1,-1,0,1},{0,1}},{{1,-1,0,1},{1,1}},{{-1,1,0,1},{0,0}},{{1,1,0,1},{1,0}}};
            id<MTLTexture> texture = eyes[eye];
            const bool isPanel = panelFrame || (compositeFrame && (!hasScene || layer > 0));
            [encoder setRenderPipelineState:isPanel?panelPipeline:pipeline];
            if (isPanel) {
                const proto::CompositeQuad* atlasQuad = compositeFrame ? &compositeQuads[layer - hasScene] : nullptr;
                const auto& quad = atlasQuad ? atlasQuad->quad : composition.quads[layer];
                if ((quad.eye_visibility == 1 && eye == 1) || (quad.eye_visibility == 2 && eye == 0)) continue;
                texture = eyes[atlasQuad ? atlasQuad->texture & 1 : layer];
                if (atlasQuad) for (auto& vertex : vertices) {
                    if (atlasQuad->texture & proto::kCompositeQuadFlipped) vertex.uv.y = 1 - vertex.uv.y;
                    vertex.uv = {(atlasQuad->x + vertex.uv.x * atlasQuad->width)/texture.width,
                                 (atlasQuad->y + vertex.uv.y * atlasQuad->height)/texture.height};
                }
                proto::Pose head; { std::lock_guard lock(inputMutex); head = latestPose.hmd; }
                if (compositeFrame) head = composition.views[eye].pose;
                auto inv = simd_inverse(quaternion(head));
                for (auto& vertex : vertices) {
                    simd_float3 point = simd_act(quaternion(quad.pose),simd_float3{vertex.position.x*quad.width/2,vertex.position.y*quad.height/2,0});
                    point += simd_float3{quad.pose.x-head.x,quad.pose.y-head.y,quad.pose.z-head.z};
                    point = simd_act(inv,point);
                    float depth = -point.z;
                    vertex.position = {point.x/(float(w/h)*.9f),point.y/.9f,depth-.01f,depth};
                }
            } else {
                float aspect = compositeFrame ? float(composite.scene_width)/composite.scene_height : float(texture.width)/texture.height;
                float sx = std::min(1.f,aspect/float(w/h)), sy = std::min(1.f,float(w/h)/aspect);
                for (auto& vertex : vertices) { vertex.position.x *= sx; vertex.position.y *= sy; }
                if (compositeFrame) for (auto& vertex : vertices) {
                    vertex.uv.x *= float(composite.scene_width)/texture.width;
                    vertex.uv.y *= float(composite.scene_height)/texture.height;
                }
            }
            [encoder setVertexBytes:vertices length:sizeof(vertices) atIndex:0];
            [encoder setFragmentTexture:texture atIndex:0];
            [encoder drawPrimitives:MTLPrimitiveTypeTriangleStrip vertexStart:0 vertexCount:4];
        }
    }
    [encoder endEncoding]; [command presentDrawable:drawable]; [command commit];
    // The next pixel upload must not overwrite a shared texture the GPU is still sampling.
    [command waitUntilCompleted];
    if (quitAfter && displayed >= quitAfter) [NSApp terminate:nil];
}
@end

@interface ReflectDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate>
@property(strong) NSWindow* window;
@property(strong) ReflectView* view;
@end
@implementation ReflectDelegate
- (void)applicationDidFinishLaunching:(NSNotification*)notification {
    (void)notification;
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device) { fprintf(stderr,"Reflect: Metal is unavailable.\n"); exit(1); }
    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0,0,1100,750) styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    self.window.title = @"Reflect — waiting for Android frames";
    self.window.delegate = self; self.window.acceptsMouseMovedEvents = YES;
    self.view = [[ReflectView alloc] initWithFrame:self.window.contentView.bounds device:device];
    self.window.contentView = self.view; [self.window center]; [self.window makeKeyAndOrderFront:nil];
    [self.window makeFirstResponder:self.view]; [NSApp activateIgnoringOtherApps:YES];
    if (!readyPath.empty()) std::ofstream(readyPath) << getpid() << "\n";
    fprintf(stderr,"Reflect: viewer ready. WASD walk, mouse look, E/Q triggers, R/F grips, Space/Z A/X, Backspace/X B/Y, M menu, C reach, T calibration, Home reset, Tab stereo, Esc release.\n");
}
- (void)windowDidResignKey:(NSNotification*)notification { (void)notification; [self.view releaseMouse]; }
- (void)windowWillClose:(NSNotification*)notification { (void)notification; [self.view releaseMouse]; [NSApp terminate:nil]; }
- (void)applicationWillTerminate:(NSNotification*)notification { (void)notification; [self.view releaseMouse]; if (!readyPath.empty()) unlink(readyPath.c_str()); }
@end

int main(int argc, char** argv) {
    int posePort=38490, imagePort=38491;
    for (int i=1;i<argc;i++) {
        std::string argument=argv[i];
        if (argument == "--help") { puts("reflect-viewer [--pose-port N] [--image-port N] [--width N] [--height N] [--fps N] [--ready-file PATH] [--capture PATH] [--quit-after-frames N]"); return 0; }
        if (++i >= argc) { fprintf(stderr,"Missing value for %s\n",argument.c_str()); return 1; }
        try {
            if (argument=="--pose-port") posePort=std::stoi(argv[i]);
            else if (argument=="--image-port") imagePort=std::stoi(argv[i]);
            else if (argument=="--width") eyeWidth=std::stoi(argv[i]);
            else if (argument=="--height") eyeHeight=std::stoi(argv[i]);
            else if (argument=="--fps") refreshRate=std::stoi(argv[i]);
            else if (argument=="--ready-file") readyPath=argv[i];
            else if (argument=="--capture") capturePath=argv[i];
            else if (argument=="--quit-after-frames") quitAfter=std::stoull(argv[i]);
            else { fprintf(stderr,"Unknown option: %s\n",argument.c_str()); return 1; }
        } catch (...) { fprintf(stderr,"Invalid value for %s\n",argument.c_str()); return 1; }
    }
    if (posePort<1024 || posePort>65535 || imagePort<1024 || imagePort>65535 || eyeWidth<64 || eyeWidth>4096 || eyeHeight<64 || eyeHeight>4096 || refreshRate<40 || refreshRate>250) return 1;
    signal(SIGPIPE,SIG_IGN);
    latestPose.hmd = {0,1.65f,0,0,0,0,1}; latestPose.hmd_flags=15;
    latestPose.render_width=eyeWidth; latestPose.render_height=eyeHeight;
    latestPose.display_period_ns=1000000000/refreshRate;
    int poseListener=listener(posePort), imageListener=listener(imagePort);
    std::thread(poses,poseListener).detach(); std::thread(images,imageListener).detach();
    @autoreleasepool {
        NSApplication* application=[NSApplication sharedApplication];
        signal(SIGTERM,SIG_IGN); signal(SIGINT,SIG_IGN);
        dispatch_source_t term = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL,SIGTERM,0,dispatch_get_main_queue());
        dispatch_source_t interrupt = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL,SIGINT,0,dispatch_get_main_queue());
        dispatch_source_set_event_handler(term,^{ [NSApp terminate:nil]; });
        dispatch_source_set_event_handler(interrupt,^{ [NSApp terminate:nil]; });
        dispatch_resume(term); dispatch_resume(interrupt);
        [application setActivationPolicy:NSApplicationActivationPolicyRegular];
        NSMenu* menu=[NSMenu new]; NSMenuItem* app=[NSMenuItem new]; [menu addItem:app];
        NSMenu* submenu=[NSMenu new]; [submenu addItemWithTitle:@"Quit Reflect" action:@selector(terminate:) keyEquivalent:@"q"];
        app.submenu=submenu; application.mainMenu=menu;
        ReflectDelegate* delegate=[ReflectDelegate new]; application.delegate=delegate;
        [application run];
        dispatch_source_cancel(term); dispatch_source_cancel(interrupt);
    }
    return 0;
}
