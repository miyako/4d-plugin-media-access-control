#include "4DPlugin-Media-Access-Control.h"
#include "PrivateTypes.h"
#include "EntryPoints.h"
#include "C_BLOB.h"
#include "C_LONGINT.h"
#include "4DPlugin-JSON.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <stdexcept>

static std::map<PA_Unichar *, size_t> strings;
static std::map<PA_Handle, size_t> handles;
struct Reference { int count; PA_Variable property; };
static std::map<void *, Reference> references;
static std::map<PA_Picture, int> pictures;
static bool nullEmpty, failedGetter, failedCommand, failHandle, failString;
static bool absent, commandMismatch;
static int platformMode;
static CUTF16String fixture;

static void check(bool ok, const char *message)
{
    if(!ok) {
        std::fprintf(stderr, "ownership assertion: %s\n", message);
        std::abort();
    }
}

static CUTF16String utf16(const char *text)
{
    CFStringRef s = CFStringCreateWithBytes(kCFAllocatorDefault,
        reinterpret_cast<const UInt8 *>(text), std::strlen(text), kCFStringEncodingUTF8, false);
    check(s != nullptr, "synthetic UTF8 decodes");
    CUTF16String result(CFStringGetLength(s), 0);
    if(!result.empty()) CFStringGetCharacters(s, CFRangeMake(0, result.size()), &result[0]);
    CFRelease(s);
    return result;
}

static PA_Unistring makeString(const PA_Unichar *text)
{
    size_t n = 0;
    if(text) while(text[n]) ++n;
    PA_Unistring result = {};
    result.fLength = n;
    if(n || !nullEmpty) {
        result.fString = static_cast<PA_Unichar *>(std::malloc((n + 1) * sizeof(PA_Unichar)));
        check(result.fString != nullptr, "host string allocation");
        if(n) std::memcpy(result.fString, text, n * sizeof(PA_Unichar));
        result.fString[n] = 0;
        PA_Unichar *key = result.fString;
        strings[key] = n;
    }
    return result;
}

static void verify(PA_Unistring s, const CUTF16String &text)
{
    check(s.fLength == text.size(), "exact UTF16 units");
    if(!s.fString) { check(text.empty(), "null only for empty"); return; }
    PA_Unichar *key = s.fString;
    check(strings.count(key) && strings.at(key) == text.size(), "live exact string capacity");
    check(s.fString[s.fLength] == 0, "terminator within allocation");
    check(CUTF16String(s.fString, s.fLength) == text, "exact UTF16 content");
}

static void dispose(PA_Unistring s)
{
    if(!s.fString) return;
    PA_Unichar *key = s.fString;
    check(strings.erase(key) == 1, "string disposed once");
    std::free(s.fString);
}

static void clear(PA_Variable *);

static void *newReference(PA_Variable property)
{
    void *p = std::malloc(1);
    references[p] = {1, property};
    return p;
}

static void retain(void *p)
{
    if(p) { check(references.count(p), "reference live"); ++references.at(p).count; }
}

static void release(void *p)
{
    if(!p) return;
    check(references.count(p), "reference released once");
    if(--references.at(p).count == 0) {
        PA_Variable property = references.at(p).property;
        references.erase(p);
        clear(&property);
        std::free(p);
    }
}

static PA_Picture newPicture()
{
    PA_Picture p = std::malloc(1);
    pictures[p] = 1;
    return p;
}

static void releasePicture(PA_Picture p)
{
    if(!p) return;
    check(pictures.count(p), "picture released once");
    if(--pictures.at(p) == 0) { pictures.erase(p); std::free(p); }
}

static PA_Variable copy(PA_Variable v)
{
    if(v.fType == eVK_Unistring) v.uValue.fString = makeString(v.uValue.fString.fString);
    else if(v.fType == eVK_Object) retain(v.uValue.fObject);
    else if(v.fType == eVK_Collection) retain(v.uValue.fCollection);
    else if(v.fType == eVK_Picture && v.uValue.fPicture) {
        PA_Picture p = v.uValue.fPicture;
        check(pictures.count(p), "copied picture live");
        ++pictures.at(p);
    }
    return v;
}

static void clear(PA_Variable *v)
{
    switch(v->fType) {
        case eVK_Unistring: dispose(v->uValue.fString); break;
        case eVK_Object: release(v->uValue.fObject); break;
        case eVK_Collection: release(v->uValue.fCollection); break;
        case eVK_Picture: releasePicture(v->uValue.fPicture); break;
        case eVK_ArrayUnicode: {
            PA_Handle h = v->uValue.fArray.fData;
            if(h) {
                check(handles.count(h), "array handle live");
                auto data = reinterpret_cast<PA_Unistring *>(*h);
                for(int i = 0; i <= v->uValue.fArray.fNbElements; ++i) dispose(data[i]);
                PA_DisposeHandle(h);
            }
            break;
        }
        case eVK_Undefined: case eVK_Boolean: case eVK_Real:
        case eVK_Longint: case eVK_Null: break;
        default: check(false, "failed output must not be cleared");
    }
    *v = {};
    v->fType = eVK_Undefined;
}

static void host(short selector, EngineBlock *eb)
{
    switch(selector) {
        case EX_CREATE_UNISTRING:
            if(failString) {
                std::memset(&eb->fUniString1, 0xa5, sizeof(eb->fUniString1));
                eb->fError = eER_IncompatibleTypes;
                failString = false;
            } else eb->fUniString1 = makeString(reinterpret_cast<PA_Unichar *>(eb->fHandle));
            break;
        case EX_DISPOSE_UNISTRING: dispose(eb->fUniString1); break;
        case EX_SET_UNISTRING: {
            auto replacement = makeString(reinterpret_cast<PA_Unichar *>(eb->fHandle));
            dispose(eb->fUniString1);
            eb->fUniString1 = replacement;
            break;
        }
        case EX_CLEAR_VARIABLE: clear(static_cast<PA_Variable *>(eb->fPtr1)); break;
        case EX_VARIABLE_TO_STRING: {
            auto v = reinterpret_cast<PA_Variable *>(eb->fHandle);
            check(v->fType == eVK_Unistring, "failed output must not be read");
            verify(v->uValue.fString, fixture);
            eb->fUniString1 = v->uValue.fString;
            break;
        }
        case EX_HANDLE_MANAGER: {
            PA_Handle h = eb->fParam1 == 1 ? nullptr : eb->fHandle;
            switch(eb->fParam1) {
                case 1:
                    if(failHandle) {
                        failHandle = false;
                        eb->fHandle = nullptr;
                        eb->fError = eER_IncompatibleTypes;
                    } else {
                        h = static_cast<PA_Handle>(std::malloc(sizeof(char *)));
                        *h = static_cast<char *>(std::calloc(1, eb->fParam2 ? eb->fParam2 : 1));
                        handles[h] = eb->fParam2;
                        eb->fHandle = h;
                    }
                    break;
                case 2:
                    check(handles.erase(h) == 1, "handle disposed once");
                    std::free(*h); std::free(h); break;
                case 3: {
                    check(handles.count(h), "resize handle live");
                    *h = static_cast<char *>(std::realloc(*h, eb->fParam2));
                    check(*h != nullptr, "host resize");
                    handles[h] = eb->fParam2;
                    break;
                }
                case 4: eb->fParam2 = handles.at(h); break;
                case 5: eb->fParam3 = h ? reinterpret_cast<sLONG_PTR>(*h) : 0; break;
                case 6: break;
                default: check(false, "unexpected handle operation");
            }
            break;
        }
        case EX_CREATE_PICTURE: eb->fPicture = newPicture(); break;
        case EX_DISPOSE_PICTURE: releasePicture(eb->fPicture); break;
        case EX_SET_OBJ_VALUE: {
            check(references.count(eb->fPtr1), "property destination live");
            auto &target = references.at(eb->fPtr1).property;
            PA_Variable replacement = copy(*static_cast<PA_Variable *>(eb->fPtr2));
            clear(&target); target = replacement;
            break;
        }
        case EX_GET_OBJ_VALUE:
            if(failedGetter) {
                std::memset(eb->fPtr2, 0xa5, sizeof(PA_Variable));
                eb->fError = eER_IncompatibleTypes;
            } else {
                check(references.count(eb->fPtr1), "property source live");
                *static_cast<PA_Variable *>(eb->fPtr2) = copy(references.at(eb->fPtr1).property);
            }
            break;
        case EX_EXECUTE_COMMAND_BY_ID: {
            auto args = static_cast<PA_Variable **>(eb->fPtr1);
            auto result = static_cast<PA_Variable *>(eb->fPtr2);
            if(eb->fParam1 == 1231) {
                check(args[1]->uValue.fString.fLength == 3, "invalid property key");
                verify(args[1]->uValue.fString, utf16("key"));
                PA_SetBooleanVariable(result, !absent);
            } else if(eb->fParam1 == 1225) {
                PA_ObjectRef obj = args[0]->uValue.fObject;
                check(references.count(obj), "duplicate source live");
                PA_SetObjectVariable(result, newReference(copy(references.at(obj).property)));
                if(platformMode == 2) failHandle = true;
            } else if(eb->fParam1 == 1217) {
                PA_ObjectRef argument = args[0]->uValue.fObject;
                check(references.count(argument), "callback argument live");
                if(failedCommand) {
                    std::memset(result, 0xa5, sizeof(*result));
                    eb->fError = eER_IncompatibleTypes;
                } else {
                    if(commandMismatch) PA_SetObjectVariable(result,
                        newReference(PA_CreateVariable(eVK_Undefined)));
                    else {
                        PA_Unistring text = makeString(fixture.c_str());
                        PA_SetStringVariable(result, &text);
                    }
                }
            } else check(false, "unexpected command");
            break;
        }
        default: check(false, "unexpected engine selector");
    }
}

// These are the only implementations linked for all identity-boundary symbols.
CFArrayRef OwnershipCopyAll(void)
{
    if(platformMode == 1) return nullptr;
    return reinterpret_cast<CFArrayRef>([[NSArray alloc] initWithObjects:@"fixture", nil]);
}
CFStringRef OwnershipAddress(SCNetworkInterfaceRef) { return platformMode == 3 ? nullptr : CFSTR("synthetic-text"); }
CFStringRef OwnershipName(SCNetworkInterfaceRef) { return platformMode == 3 ? nullptr : CFSTR("en0"); }
CFStringRef OwnershipType(SCNetworkInterfaceRef) { return platformMode == 3 ? nullptr : CFSTR("synthetic-type"); }
CFStringRef OwnershipDisplay(SCNetworkInterfaceRef) { return platformMode == 3 ? nullptr : CFSTR("synthetic-display"); }

static void balanced(const char *label)
{
    check(strings.empty(), label);
    check(handles.empty(), "all handles balanced");
    check(references.empty(), "all object collection references balanced");
    check(pictures.empty(), "all picture references balanced");
}

static PA_ObjectRef object(PA_VariableKind kind)
{
    PA_Variable v = PA_CreateVariable(eVK_Undefined);
    if(kind == eVK_Unistring) {
        PA_Unistring s = makeString(fixture.c_str());
        PA_SetStringVariable(&v, &s);
    } else if(kind == eVK_Object) PA_SetObjectVariable(&v, newReference(v));
    else if(kind == eVK_Collection) PA_SetCollectionVariable(&v, newReference(v));
    else if(kind == eVK_Boolean) PA_SetBooleanVariable(&v, 1);
    else if(kind == eVK_Real) PA_SetRealVariable(&v, 2.5);
    return newReference(v);
}

static void setters(int which)
{
    PA_ObjectRef obj = object(eVK_Undefined);
    const char *texts[] = {"ASCII", "\xce\xa9", "\xf0\x9f\x98\x80", ""};
    const wchar_t *wide[] = {L"ASCII", L"\u03a9", L"\U0001f600", L""};
    for(int i = 0; i < 4 && which < 4; ++i) {
        fixture = utf16(texts[i]);
        if(which == 0) ob_set_s(obj, "key", texts[i]);
        if(which == 1) ob_set_s(obj, L"key", texts[i]);
        if(which == 2) ob_set_a(obj, L"key", wide[i]);
        if(which == 3) ob_set_a(obj, L"key", &fixture);
        verify(references.at(obj).property.uValue.fString, fixture);
    }
    if(which == 4) ob_set_p(obj, L"key", newPicture());
    PA_DisposeObject(obj);
    balanced("setter initial owner leaked");
}

static void getters(const std::string &name)
{
    fixture = utf16("ASCII \xce\xa9 \xf0\x9f\x98\x80");
    bool mismatch = name.find("mismatch") != std::string::npos;
    bool failure = name.find("failed") != std::string::npos;
    PA_VariableKind kind = eVK_Unistring;
    if(!mismatch && name == "get-b") kind = eVK_Boolean;
    if(!mismatch && name == "get-n") kind = eVK_Real;
    if(!mismatch && name == "get-o") kind = eVK_Object;
    if(!mismatch && name == "get-c") kind = eVK_Collection;
    if(name == "get-a-mismatch") kind = eVK_Object;
    PA_ObjectRef obj = object(kind);
    CUTF8String out(reinterpret_cast<const uint8_t *>("unchanged"));
    failedGetter = failure;
    if(name == "get-a-has-failed") { failedGetter = false; failHandle = true; }
    failString = name == "failed-key";
    bool threw = false;
    try {
        if(name == "failed-key" || name.find("get-a") == 0) {
            check(ob_get_a(obj, L"key", &out), "existing property");
            if(!mismatch && !failure) check(out == CUTF8String(reinterpret_cast<const uint8_t *>(
                "ASCII \xce\xa9 \xf0\x9f\x98\x80")), "exact UTF8 bytes");
        } else if(name.find("get-b") == 0) {
            check(ob_get_b(obj, L"key") == (!mismatch && !failure), "boolean copied");
        } else if(name.find("get-n") == 0) {
            check(ob_get_n(obj, L"key") == (mismatch || failure ? 0.0 : 2.5), "number copied");
        } else if(name.find("get-o") == 0) {
            PA_ObjectRef result = ob_get_o(obj, L"key");
            if(!mismatch && !failure) {
                PA_DisposeObject(obj); obj = nullptr;
                check(references.count(result), "owning object handoff survives source");
                PA_DisposeObject(result);
            } else check(!result, "mismatched object result");
        } else if(name.find("get-c") == 0) {
            PA_CollectionRef result = ob_get_c(obj, L"key");
            if(!mismatch && !failure) {
                PA_DisposeObject(obj); obj = nullptr;
                check(references.count(result), "owning collection handoff survives source");
                PA_DisposeCollection(result);
            } else check(!result, "mismatched collection result");
        }
    } catch(const std::runtime_error &) { threw = true; }
    check(threw == failure, "failed property output reported before inspection");
    if(failure) check(out == CUTF8String(reinterpret_cast<const uint8_t *>("unchanged")),
                      "failure preserves caller bytes");
    PA_DisposeObject(obj);
    balanced("getter owned output leaked");
}

static void stringify(const std::string &name)
{
    fixture = utf16("ASCII \xce\xa9 \xf0\x9f\x98\x80");
    PA_ObjectRef obj = object(eVK_Unistring);
    CUTF8String out(reinterpret_cast<const uint8_t *>("unchanged"));
    failedCommand = name == "stringify-failed";
    commandMismatch = name == "stringify-mismatch";
    failHandle = name == "stringify-copy-failed";
    // Fail only the second command's parameter handle, after OB Copy succeeds.
    bool threw = false;
    if(name == "stringify-sentinel") platformMode = 2;
    try { ob_stringify(obj, &out); }
    catch(const std::runtime_error &) { threw = true; }
    if(name == "stringify-mismatch")
        check(!threw && out == CUTF8String(reinterpret_cast<const uint8_t *>("unchanged")),
              "valid mismatched stringify result cleared");
    else if(name == "stringify") check(!threw && out == CUTF8String(
        reinterpret_cast<const uint8_t *>("ASCII \xce\xa9 \xf0\x9f\x98\x80")), "stringify exact UTF8");
    else {
        check(threw, "failed stringify reported before inspection");
        check(out == CUTF8String(reinterpret_cast<const uint8_t *>("unchanged")),
              "failed stringify preserves caller bytes");
    }
    PA_DisposeObject(obj);
    balanced("stringify owners balanced");
}

static void arrays()
{
    PA_Variable destination = PA_CreateVariable(eVK_ArrayUnicode);
    PA_ResizeArray(&destination, 3);
    const char *texts[] = {"ASCII", "\xce\xa9", "\xf0\x9f\x98\x80", ""};
    for(int repeat = 0; repeat < 3; ++repeat) {
        for(int i = 0; i < 4; ++i) {
            CUTF16String text = utf16(texts[i]);
            PA_Unistring owned = PA_CreateUnistring(const_cast<PA_Unichar *>(text.c_str()));
            PA_SetStringInArray(destination, i, &owned);
            verify(PA_GetStringInArray(destination, i), text);
        }
    }
    ARRAY_TEXT copied;
    BytePtr params[] = {reinterpret_cast<BytePtr>(&destination)};
    copied.fromParamAtIndex(params, 1);
    PA_ClearVariable(&destination);
    destination = PA_CreateVariable(eVK_Undefined);
    copied.toParamAtIndex(params, 1);
    for(int i = 0; i < 4; ++i) verify(PA_GetStringInArray(destination, i), utf16(texts[i]));
    PA_Unistring rejected = makeString(utf16("rejected").c_str());
    PA_Variable wrong = PA_CreateVariable(eVK_Longint);
    PA_Variable noData = {};
    noData.fType = eVK_ArrayUnicode;
    PA_SetStringInArray(destination, -1, &rejected);
    PA_SetStringInArray(destination, 4, &rejected);
    PA_SetStringInArray(wrong, 0, &rejected);
    PA_SetStringInArray(noData, 0, &rejected);
    PA_SetStringInArray(destination, 0, nullptr);
    verify(rejected, utf16("rejected"));
    PA_DisposeUnistring(&rejected);
    PA_ResizeArray(&destination, 0);
    verify(PA_GetStringInArray(destination, 0), utf16("ASCII"));
    PA_ClearVariable(&destination);
    balanced("array transfers borrowed copies replacement shrink clear");
}

static void scalars()
{
    for(const char *text : {"ASCII", "\xce\xa9", "\xf0\x9f\x98\x80", ""}) {
        CUTF16String expected = utf16(text);
        PA_Unistring result = makeString(nullptr);
        PA_Unistring *slot = &result;
        {
            C_TEXT value;
            CUTF8String bytes(reinterpret_cast<const uint8_t *>(text));
            value.setUTF8String(&bytes);
            check(value.getUTF16Length() == expected.size(), "scalar exact units");
            check(CUTF16String(value.getUTF16StringPtr(), value.getUTF16Length()) == expected,
                  "scalar exact text");
            check(value.getUTF16StringPtr()[value.getUTF16Length()] == 0, "scalar terminator");
            CUTF8String roundtrip;
            value.copyUTF8String(&roundtrip);
            check(bytes == roundtrip, "scalar exact UTF8 bytes");
            value.setReturn(reinterpret_cast<sLONG_PTR *>(&slot));
        }
        verify(result, expected);
        PA_DisposeUnistring(&result);
        PA_Unistring independent = makeString(expected.c_str());
        PA_Variable argument = PA_CreateVariable(eVK_Undefined);
        PA_SetStringVariable(&argument, &independent);
        check(argument.uValue.fString.fString == independent.fString, "scalar shallow alias");
        PA_ClearVariable(&argument);
        result = makeString(nullptr);
        PluginBlock block = {};
        block.fResult = &slot;
        PA_ReturnString(&block, const_cast<PA_Unichar *>(expected.c_str()));
        verify(result, expected);
        PA_DisposeUnistring(&result);
    }
    balanced("scalar content controls");
}

static void dispatch()
{
    for(int mode : {0, 1, 3}) {
        platformMode = mode;
        PA_Variable a[4];
        BytePtr params[4];
        for(int i = 0; i < 4; ++i) {
            a[i] = PA_CreateVariable(eVK_ArrayUnicode);
            params[i] = reinterpret_cast<BytePtr>(&a[i]);
        }
        PA_Unistring result = makeString(nullptr);
        PA_Unistring *slot = &result;
        void *data = nullptr;
        FourDPackex(1, params, &data, &slot);
        const char *texts[] = {"synthetic-text", "synthetic-type", "en0", "synthetic-display"};
        for(int i = 0; i < 4; ++i) {
            check(a[i].uValue.fArray.fNbElements == (mode == 1 ? 0 : 1), "dispatcher output count");
            verify(PA_GetStringInArray(a[i], 0), utf16(""));
            if(mode != 1) verify(PA_GetStringInArray(a[i], 1), utf16(mode == 3 ? "" : texts[i]));
            PA_ClearVariable(&a[i]);
        }
        verify(result, utf16(mode ? "" : "synthetic-text"));
        PA_DisposeUnistring(&result);
    }
    balanced("synthetic dispatcher owners");
}

static void missing()
{
    absent = true;
    PA_ObjectRef obj = object(eVK_Unistring);
    CUTF8String out(reinterpret_cast<const uint8_t *>("unchanged"));
    check(!ob_get_a(obj, L"key", &out), "missing text property");
    check(!ob_get_b(obj, L"key") && ob_get_n(obj, L"key") == 0.0, "missing scalar property");
    check(!ob_get_o(obj, L"key") && !ob_get_c(obj, L"key"), "missing reference property");
    check(out == CUTF8String(reinterpret_cast<const uint8_t *>("unchanged")), "missing preserves bytes");
    PA_DisposeObject(obj);
    balanced("missing properties balanced");
}

static void deferredCreation()
{
    PA_Variable destination = {};
    destination.fType = eVK_Undefined;
    PA_Variable original = destination;
    BytePtr params[] = {reinterpret_cast<BytePtr>(&destination)};
    ARRAY_TEXT source;
    source.appendUTF16String(utf16("synthetic").c_str());
    failHandle = true;
    source.toParamAtIndex(params, 1);
    check(std::memcmp(&destination, &original, sizeof(destination)) == 0,
          "DEFERRED initial array create publishes success-shaped descriptor");
}

int main(int argc, char **argv)
{
    check(argc == 3, "case and empty representation required");
    @autoreleasepool {
        gCall4D = host;
        nullEmpty = std::string(argv[2]) == "null";
        std::string name = argv[1];
        if(name.find("set-") == 0) setters(std::atoi(argv[1] + 4));
        else if(name.find("get-") == 0 || name == "failed-key") getters(name);
        else if(name.find("stringify") == 0) stringify(name);
        else if(name == "arrays") arrays();
        else if(name == "scalars") scalars();
        else if(name == "dispatch") dispatch();
        else if(name == "missing") missing();
        else if(name == "deferred-create") deferredCreation();
        else check(false, "unknown test");
    }
    std::printf("PASS %s/%s\n", argv[1], argv[2]);
}
