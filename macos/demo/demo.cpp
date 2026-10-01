// Small, freely redistributable OpenXR APK for exercising Reflect's complete path.
#define XR_USE_PLATFORM_ANDROID
#define XR_USE_GRAPHICS_API_OPENGL_ES
#include <jni.h>
#include <EGL/egl.h>
#include <GLES3/gl3.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <android/log.h>
#include <android_native_app_glue.h>
#include <array>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <thread>
#include <chrono>
#include <vector>

static void check(XrResult result, const char* operation) {
    if (XR_FAILED(result)) {
        __android_log_print(ANDROID_LOG_ERROR,"Reflect.Demo","%s failed: %d",operation,result);
        throw std::runtime_error(operation);
    }
}
#define XR(call) check(call,#call)
using Matrix = std::array<float,16>;
static Matrix identity() { return {1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1}; }
static Matrix multiply(const Matrix& a, const Matrix& b) {
    Matrix result{};
    for (int c=0;c<4;c++) for (int r=0;r<4;r++) for (int k=0;k<4;k++) result[c*4+r] += a[k*4+r]*b[c*4+k];
    return result;
}
static Matrix view_projection(const XrView& view) {
    auto q=view.pose.orientation;
    float x=-q.x,y=-q.y,z=-q.z,w=q.w;
    Matrix rotation={1-2*y*y-2*z*z,2*x*y+2*w*z,2*x*z-2*w*y,0,
        2*x*y-2*w*z,1-2*x*x-2*z*z,2*y*z+2*w*x,0,
        2*x*z+2*w*y,2*y*z-2*w*x,1-2*x*x-2*y*y,0,0,0,0,1};
    Matrix translation=identity();
    translation[12]=-view.pose.position.x; translation[13]=-view.pose.position.y; translation[14]=-view.pose.position.z;
    float l=std::tan(view.fov.angleLeft),r=std::tan(view.fov.angleRight);
    float d=std::tan(view.fov.angleDown),u=std::tan(view.fov.angleUp),n=.05f,f=100.f;
    Matrix projection={2/(r-l),0,0,0,0,2/(u-d),0,0,(r+l)/(r-l),(u+d)/(u-d),-(f+n)/(f-n),-1,0,0,-2*f*n/(f-n),0};
    return multiply(projection,multiply(rotation,translation));
}
static GLuint shader(GLenum kind,const char* source) {
    GLuint s=glCreateShader(kind); glShaderSource(s,1,&source,nullptr); glCompileShader(s);
    GLint success=0; glGetShaderiv(s,GL_COMPILE_STATUS,&success);
    if (!success) throw std::runtime_error("GL shader compilation");
    return s;
}
static void demo(android_app* app) {
    JNIEnv* environment = nullptr;
    app->activity->vm->AttachCurrentThread(&environment,nullptr);
    PFN_xrInitializeLoaderKHR initialize=nullptr;
    XR(xrGetInstanceProcAddr(XR_NULL_HANDLE,"xrInitializeLoaderKHR",reinterpret_cast<PFN_xrVoidFunction*>(&initialize)));
    XrLoaderInitInfoAndroidKHR init{XR_TYPE_LOADER_INIT_INFO_ANDROID_KHR};
    init.applicationVM=app->activity->vm; init.applicationContext=app->activity->clazz;
    XR(initialize(reinterpret_cast<XrLoaderInitInfoBaseHeaderKHR*>(&init)));
    const char* extensions[]={XR_KHR_ANDROID_CREATE_INSTANCE_EXTENSION_NAME,XR_KHR_OPENGL_ES_ENABLE_EXTENSION_NAME};
    XrInstanceCreateInfoAndroidKHR android{XR_TYPE_INSTANCE_CREATE_INFO_ANDROID_KHR};
    android.applicationVM=app->activity->vm; android.applicationActivity=app->activity->clazz;
    XrInstanceCreateInfo create{XR_TYPE_INSTANCE_CREATE_INFO}; create.next=&android;
    strcpy(create.applicationInfo.applicationName,"Reflect Demo"); create.applicationInfo.apiVersion=XR_MAKE_VERSION(1,0,0);
    create.enabledExtensionCount=2; create.enabledExtensionNames=extensions;
    XrInstance instance; XR(xrCreateInstance(&create,&instance));
    XrSystemGetInfo systemInfo{XR_TYPE_SYSTEM_GET_INFO}; systemInfo.formFactor=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XrSystemId system; XR(xrGetSystem(instance,&systemInfo,&system));
    PFN_xrGetOpenGLESGraphicsRequirementsKHR requirements;
    XR(xrGetInstanceProcAddr(instance,"xrGetOpenGLESGraphicsRequirementsKHR",reinterpret_cast<PFN_xrVoidFunction*>(&requirements)));
    XrGraphicsRequirementsOpenGLESKHR graphics{XR_TYPE_GRAPHICS_REQUIREMENTS_OPENGL_ES_KHR}; XR(requirements(instance,system,&graphics));

    EGLDisplay display=eglGetDisplay(EGL_DEFAULT_DISPLAY); eglInitialize(display,nullptr,nullptr);
    EGLint attributes[]={EGL_RENDERABLE_TYPE,EGL_OPENGL_ES3_BIT,EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_ALPHA_SIZE,8,EGL_NONE};
    EGLConfig config; EGLint count; eglChooseConfig(display,attributes,&config,1,&count);
    EGLint contextAttributes[]={EGL_CONTEXT_CLIENT_VERSION,3,EGL_NONE};
    EGLContext context=eglCreateContext(display,config,EGL_NO_CONTEXT,contextAttributes);
    EGLint pbufferAttributes[]={EGL_WIDTH,1,EGL_HEIGHT,1,EGL_NONE};
    EGLSurface surface=eglCreatePbufferSurface(display,config,pbufferAttributes);
    if (!eglMakeCurrent(display,surface,surface,context)) throw std::runtime_error("EGL context");
    __android_log_print(ANDROID_LOG_INFO,"Reflect.Demo","GPU: %s",glGetString(GL_RENDERER));
    XrGraphicsBindingOpenGLESAndroidKHR binding{XR_TYPE_GRAPHICS_BINDING_OPENGL_ES_ANDROID_KHR};
    binding.display=display; binding.config=config; binding.context=context;
    XrSessionCreateInfo sessionInfo{XR_TYPE_SESSION_CREATE_INFO}; sessionInfo.next=&binding; sessionInfo.systemId=system;
    XrSession session; XR(xrCreateSession(instance,&sessionInfo,&session));
    XrReferenceSpaceCreateInfo spaceInfo{XR_TYPE_REFERENCE_SPACE_CREATE_INFO};
    spaceInfo.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_LOCAL; spaceInfo.poseInReferenceSpace.orientation.w=1;
    XrSpace space; XR(xrCreateReferenceSpace(session,&spaceInfo,&space));

    XrActionSetCreateInfo setInfo{XR_TYPE_ACTION_SET_CREATE_INFO};
    strcpy(setInfo.actionSetName,"demo"); strcpy(setInfo.localizedActionSetName,"Demo");
    XrActionSet actionSet; XR(xrCreateActionSet(instance,&setInfo,&actionSet));
    XrActionCreateInfo actionInfo{XR_TYPE_ACTION_CREATE_INFO};
    actionInfo.actionType=XR_ACTION_TYPE_FLOAT_INPUT; strcpy(actionInfo.actionName,"trigger"); strcpy(actionInfo.localizedActionName,"Trigger");
    XrAction trigger; XR(xrCreateAction(actionSet,&actionInfo,&trigger));
    XrPath profile,triggerPath;
    XR(xrStringToPath(instance,"/interaction_profiles/oculus/touch_controller",&profile));
    XR(xrStringToPath(instance,"/user/hand/right/input/trigger/value",&triggerPath));
    XrActionSuggestedBinding suggested{trigger,triggerPath};
    XrInteractionProfileSuggestedBinding suggestions{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING};
    suggestions.interactionProfile=profile; suggestions.countSuggestedBindings=1; suggestions.suggestedBindings=&suggested;
    XR(xrSuggestInteractionProfileBindings(instance,&suggestions));
    XrSessionActionSetsAttachInfo attach{XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO}; attach.countActionSets=1; attach.actionSets=&actionSet;
    XR(xrAttachSessionActionSets(session,&attach));

    uint32_t viewCount=0; XR(xrEnumerateViewConfigurationViews(instance,system,XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,0,&viewCount,nullptr));
    std::vector<XrViewConfigurationView> configurations(viewCount,{XR_TYPE_VIEW_CONFIGURATION_VIEW});
    XR(xrEnumerateViewConfigurationViews(instance,system,XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,viewCount,&viewCount,configurations.data()));
    if (viewCount!=2) throw std::runtime_error("Expected stereo runtime");
    XrSwapchain swapchains[2]; std::vector<XrSwapchainImageOpenGLESKHR> images[2];
    for (int eye=0;eye<2;eye++) {
        XrSwapchainCreateInfo info{XR_TYPE_SWAPCHAIN_CREATE_INFO}; info.usageFlags=XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
        info.format=GL_RGBA8; info.sampleCount=1; info.width=configurations[eye].recommendedImageRectWidth; info.height=configurations[eye].recommendedImageRectHeight;
        info.faceCount=info.arraySize=info.mipCount=1;
        XR(xrCreateSwapchain(session,&info,&swapchains[eye]));
        uint32_t imageCount; XR(xrEnumerateSwapchainImages(swapchains[eye],0,&imageCount,nullptr));
        images[eye].resize(imageCount,{XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_ES_KHR});
        XR(xrEnumerateSwapchainImages(swapchains[eye],imageCount,&imageCount,reinterpret_cast<XrSwapchainImageBaseHeader*>(images[eye].data())));
    }
    GLuint program=glCreateProgram();
    glAttachShader(program,shader(GL_VERTEX_SHADER,"#version 300 es\nlayout(location=0) in vec3 p; uniform mat4 mvp; out float shade; void main(){gl_Position=mvp*vec4(p,1);shade=.65+.35*(p.y+.5);}"));
    glAttachShader(program,shader(GL_FRAGMENT_SHADER,"#version 300 es\nprecision highp float; in float shade; uniform vec3 color; out vec4 c; void main(){c=vec4(color*shade,1);}"));
    glLinkProgram(program); glUseProgram(program);
    GLint mvp=glGetUniformLocation(program,"mvp"),color=glGetUniformLocation(program,"color");
    constexpr float cube[]={
        -.5,-.5,.5,.5,-.5,.5,.5,.5,.5,-.5,-.5,.5,.5,.5,.5,-.5,.5,.5,
        .5,-.5,-.5,-.5,-.5,-.5,-.5,.5,-.5,.5,-.5,-.5,-.5,.5,-.5,.5,.5,-.5,
        -.5,-.5,-.5,-.5,-.5,.5,-.5,.5,.5,-.5,-.5,-.5,-.5,.5,.5,-.5,.5,-.5,
        .5,-.5,.5,.5,-.5,-.5,.5,.5,-.5,.5,-.5,.5,.5,.5,-.5,.5,.5,.5,
        -.5,.5,.5,.5,.5,.5,.5,.5,-.5,-.5,.5,.5,.5,.5,-.5,-.5,.5,-.5,
        -.5,-.5,-.5,.5,-.5,-.5,.5,-.5,.5,-.5,-.5,-.5,.5,-.5,.5,-.5,-.5,.5};
    GLuint vao,buffer,fbo,depth; glGenVertexArrays(1,&vao); glBindVertexArray(vao);
    glGenBuffers(1,&buffer); glBindBuffer(GL_ARRAY_BUFFER,buffer); glBufferData(GL_ARRAY_BUFFER,sizeof(cube),cube,GL_STATIC_DRAW);
    glEnableVertexAttribArray(0); glVertexAttribPointer(0,3,GL_FLOAT,GL_FALSE,0,nullptr);
    glGenFramebuffers(1,&fbo); glGenRenderbuffers(1,&depth);
    glBindRenderbuffer(GL_RENDERBUFFER,depth); glRenderbufferStorage(GL_RENDERBUFFER,GL_DEPTH_COMPONENT24,configurations[0].recommendedImageRectWidth,configurations[0].recommendedImageRectHeight);
    bool running=false; unsigned frameNumber=0; float previousTrigger=-1;
    while (!app->destroyRequested) {
        int events; android_poll_source* source;
        while (ALooper_pollOnce(0,nullptr,&events,reinterpret_cast<void**>(&source))>=0) if (source) source->process(app,source);
        XrEventDataBuffer event{XR_TYPE_EVENT_DATA_BUFFER};
        while (xrPollEvent(instance,&event)==XR_SUCCESS) {
            if (event.type==XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
                auto state=reinterpret_cast<XrEventDataSessionStateChanged*>(&event)->state;
                if (state==XR_SESSION_STATE_READY) { XrSessionBeginInfo begin{XR_TYPE_SESSION_BEGIN_INFO}; begin.primaryViewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO; XR(xrBeginSession(session,&begin)); running=true; }
                if (state==XR_SESSION_STATE_STOPPING) { XR(xrEndSession(session)); running=false; }
                if (state==XR_SESSION_STATE_EXITING || state==XR_SESSION_STATE_LOSS_PENDING) break;
            }
            event={XR_TYPE_EVENT_DATA_BUFFER};
        }
        if (!running) { std::this_thread::sleep_for(std::chrono::milliseconds(10)); continue; }
        XrActiveActionSet active{actionSet,XR_NULL_PATH};
        XrActionsSyncInfo sync{XR_TYPE_ACTIONS_SYNC_INFO}; sync.countActiveActionSets=1; sync.activeActionSets=&active; XR(xrSyncActions(session,&sync));
        XrActionStateGetInfo get{XR_TYPE_ACTION_STATE_GET_INFO}; get.action=trigger;
        XrActionStateFloat triggerState{XR_TYPE_ACTION_STATE_FLOAT}; XR(xrGetActionStateFloat(session,&get,&triggerState));
        if (triggerState.currentState!=previousTrigger) {
            previousTrigger=triggerState.currentState;
            __android_log_print(ANDROID_LOG_INFO,"Reflect.Demo","right trigger=%.1f active=%u",previousTrigger,triggerState.isActive);
        }
        XrFrameWaitInfo wait{XR_TYPE_FRAME_WAIT_INFO}; XrFrameState state{XR_TYPE_FRAME_STATE}; XR(xrWaitFrame(session,&wait,&state));
        XrFrameBeginInfo begin{XR_TYPE_FRAME_BEGIN_INFO}; XR(xrBeginFrame(session,&begin));
        XrViewLocateInfo locate{XR_TYPE_VIEW_LOCATE_INFO}; locate.viewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO; locate.displayTime=state.predictedDisplayTime; locate.space=space;
        XrViewState viewState{XR_TYPE_VIEW_STATE}; XrView views[2]={{XR_TYPE_VIEW},{XR_TYPE_VIEW}};
        XR(xrLocateViews(session,&locate,&viewState,2,&viewCount,views));
        XrCompositionLayerProjectionView layers[2]={{XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW},{XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW}};
        for (int eye=0;eye<2;eye++) {
            XrSwapchainImageAcquireInfo acquire{XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO}; uint32_t index; XR(xrAcquireSwapchainImage(swapchains[eye],&acquire,&index));
            XrSwapchainImageWaitInfo imageWait{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO}; imageWait.timeout=XR_INFINITE_DURATION; XR(xrWaitSwapchainImage(swapchains[eye],&imageWait));
            glBindFramebuffer(GL_FRAMEBUFFER,fbo); glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,images[eye][index].image,0);
            glFramebufferRenderbuffer(GL_FRAMEBUFFER,GL_DEPTH_ATTACHMENT,GL_RENDERBUFFER,depth);
            glViewport(0,0,configurations[eye].recommendedImageRectWidth,configurations[eye].recommendedImageRectHeight);
            glEnable(GL_DEPTH_TEST); glClearColor(.035,.06,.11,1); glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT);
            const auto vp=view_projection(views[eye]);
            auto box=[&](float x,float y,float z,float sx,float sy,float sz,float r,float g,float b) {
                Matrix model=identity(); model[0]=sx;model[5]=sy;model[10]=sz;model[12]=x;model[13]=y;model[14]=z;
                auto matrix=multiply(vp,model); glUniformMatrix4fv(mvp,1,GL_FALSE,matrix.data()); glUniform3f(color,r,g,b); glDrawArrays(GL_TRIANGLES,0,36);
            };
            box(0,-.05,-5,20,.1,20,.13,.18,.24);
            for (int j=0;j<10;j++) {
                box(float(j)-4.5f,0,-5,.025,.02,20,.25,.35,.45);
                box(0,0,float(j)-9.5f,20,.02,.025,.25,.35,.45);
            }
            box(0,1.25,-3,1,1,1,previousTrigger>.5?1.f:.15f,previousTrigger>.5?.3f:.8f,previousTrigger>.5?.08f:1.f);
            box(-2,.5,-5,1,1,1,.8,.2,.45); box(2,.75,-6,1,1.5,1,.45,.8,.2);
            glFlush();
            XrSwapchainImageReleaseInfo release{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO}; XR(xrReleaseSwapchainImage(swapchains[eye],&release));
            layers[eye].pose=views[eye].pose; layers[eye].fov=views[eye].fov; layers[eye].subImage.swapchain=swapchains[eye];
            layers[eye].subImage.imageRect.extent={int(configurations[eye].recommendedImageRectWidth),int(configurations[eye].recommendedImageRectHeight)};
        }
        XrCompositionLayerProjection projection{XR_TYPE_COMPOSITION_LAYER_PROJECTION}; projection.space=space; projection.viewCount=2; projection.views=layers;
        const XrCompositionLayerBaseHeader* header=reinterpret_cast<XrCompositionLayerBaseHeader*>(&projection);
        XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO}; end.displayTime=state.predictedDisplayTime; end.environmentBlendMode=XR_ENVIRONMENT_BLEND_MODE_OPAQUE; end.layerCount=state.shouldRender?1:0; end.layers=&header;
        XR(xrEndFrame(session,&end));
        if (++frameNumber%180==1) __android_log_print(ANDROID_LOG_INFO,"Reflect.Demo","frame=%u head=(%.2f,%.2f,%.2f)",frameNumber,views[0].pose.position.x,views[0].pose.position.y,views[0].pose.position.z);
    }
    xrDestroySession(session); xrDestroyInstance(instance);
}
extern "C" void android_main(android_app* app) {
    try { demo(app); }
    catch (const std::exception& error) { __android_log_print(ANDROID_LOG_ERROR,"Reflect.Demo","%s",error.what()); ANativeActivity_finish(app->activity); }
}
