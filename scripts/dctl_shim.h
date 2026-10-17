/*
 * Shim for compiling OpenDRT DCTLs outside of Resolve.
 *
 * A DCTL is written for Resolve's runtime, which compiles it as C++-flavored
 * CUDA: float2/float3 are vector structs with component-wise arithmetic,
 * math intrinsics carry a leading underscore (_powf, _fmod, ...), and the
 * DEFINE_UI_PARAMS(...) wrappers become UI widgets. The bake pipeline
 * (scripts/20-bake-luts.py) pastes this header in front of the DCTL body,
 * so exactly those three contract pieces are supplied:
 *
 *   * float2 / float3 structs with component-wise + - * / (both operand
 *     orders, mixed with scalars), compound assignment, unary minus,
 *   * <math.h> behind the underscore aliases Resolve provides,
 *   * no-ops for the UI wrapper macros. Live DEFINE_UI_PARAMS lines are
 *     rewritten into plain globals by the python preprocess; a line that
 *     escapes that rewrite expands to an undefined identifier, so the
 *     compile fails loudly instead of silently baking different math.
 */

#ifndef OPENDRT_DCTL_SHIM_HPP
#define OPENDRT_DCTL_SHIM_HPP

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* DCTL function decoration and host-provided compile-time constants. */
#define __DEVICE__   inline
#define __CONSTANT__ static const

/* The tooltip block is guarded on Resolve >= 19.1; pretend to be recent. */
#define __RESOLVE_VER_MAJOR__ 99
#define __RESOLVE_VER_MINOR__ 9
#define DEFINE_UI_TOOLTIP(label, text)  /* tooltips are UI, not math */
#define DEFINE_UI_PARAMS(var, label, kind, def, ...) UNHANDLED_DEFINE_UI_PARAMS(kind)

/* Resolve's vector types. Default-constructing keeps local declarations
 * order-safe; element-wise operators mirror CUDA semantics. */
struct float2 {
    float x = 0, y = 0;
};
struct float3 {
    float x = 0, y = 0, z = 0;
};

inline float2 make_float2(float x, float y) { return {x, y}; }
inline float3 make_float3(float x, float y, float z) { return {x, y, z}; }

inline float2 operator+(float2 a, float2 b) { return {a.x + b.x, a.y + b.y}; }
inline float2 operator-(float2 a, float2 b) { return {a.x - b.x, a.y - b.y}; }
inline float2 operator*(float2 a, float2 b) { return {a.x * b.x, a.y * b.y}; }
inline float2 operator/(float2 a, float2 b) { return {a.x / b.x, a.y / b.y}; }
inline float2 operator*(float2 a, float s) { return {a.x * s, a.y * s}; }
inline float2 operator*(float s, float2 a) { return a * s; }
inline float2 operator+(float2 a, float s) { return {a.x + s, a.y + s}; }
inline float2 operator+(float s, float2 a) { return a + s; }
inline float2 operator-(float2 a, float s) { return {a.x - s, a.y - s}; }
inline float2 operator-(float s, float2 a) { return {s - a.x, s - a.y}; }
inline float2 operator+=(float2 &a, float2 b) { a = a + b; return a; }
inline float2 operator-=(float2 &a, float2 b) { a = a - b; return a; }
inline float2 operator*=(float2 &a, float2 b) { a = a * b; return a; }
inline float2 operator*=(float2 &a, float s) { a = a * s; return a; }
inline float2 operator-(float2 a) { return {-a.x, -a.y}; }

inline float3 operator+(float3 a, float3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline float3 operator-(float3 a, float3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline float3 operator*(float3 a, float3 b) { return {a.x * b.x, a.y * b.y, a.z * b.z}; }
inline float3 operator/(float3 a, float3 b) { return {a.x / b.x, a.y / b.y, a.z / b.z}; }
inline float3 operator*(float3 a, float s)  { return {a.x * s, a.y * s, a.z * s}; }
inline float3 operator*(float s, float3 a)  { return a * s; }
inline float3 operator/(float3 a, float s)  { return {a.x / s, a.y / s, a.z / s}; }
inline float3 operator+(float3 a, float s)  { return {a.x + s, a.y + s, a.z + s}; }
inline float3 operator+(float s, float3 a)  { return a + s; }
inline float3 operator-(float3 a, float s)  { return {a.x - s, a.y - s, a.z - s}; }
inline float3 operator-(float s, float3 a)  { return {s - a.x, s - a.y, s - a.z}; }
inline float3 operator+=(float3 &a, float3 b) { a = a + b; return a; }
inline float3 operator-=(float3 &a, float3 b) { a = a - b; return a; }
inline float3 operator*=(float3 &a, float3 b) { a = a * b; return a; }
inline float3 operator*=(float3 &a, float s)  { a = a * s; return a; }
inline float3 operator/=(float3 &a, float s)  { a = a / s; return a; }
inline float3 operator+=(float3 &a, float s) { a = a + s; return a; }
inline float3 operator-=(float3 &a, float s) { a = a - s; return a; }
inline float2 operator+=(float2 &a, float s) { a = a + s; return a; }
inline float3 operator-(float3 a) { return {-a.x, -a.y, -a.z}; }

/* Resolve math intrinsics -> standard libm/glibc equivalents (glibc ships
 * exp10f; every other one is a plain alias). */
#define _fabs(x) fabsf(x)
#define _fmaxf   fmaxf
#define _fminf   fminf
#define _fmod    fmodf
#define _atan2f  atan2f
#define _expf    expf
#define _exp10f  exp10f
#define _exp2f   exp2f
#define _logf    logf
#define _log2f   log2f
#define _powf    powf
#define _sqrtf   sqrtf

#endif /* OPENDRT_DCTL_SHIM_HPP */