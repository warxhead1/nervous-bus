// NeuralReaction.slang — StructuredBuffer MLP inference for evolved physics kernels
//
// Deploys a distill_reaction.py-trained MLP in a WGPU/Vulkan compute shader.
// Architecture: [N_in → 32 → 32 → 1], ReLU activations, float16 weights.
//
// Weight layout (from distill_reaction.py):
//   W1[N_in × 32]  b1[32]   — input → hidden1
//   W2[32 × 32]    b2[32]   — hidden1 → hidden2
//   W3[1 × 32]     b3[1]    — hidden2 → output
//
// Upload from Python:
//   weights = np.load("phase_mlp_<ts>.npy").astype(np.float16)
//   # create StructuredBuffer<float16_t> of size len(weights), upload
//
// When to use MLP vs LUT (bake_kernel_lut.py output):
//   LUT  — fixed kernel, baked offline, ~128KB VRAM, pure hardware texture fetch
//   MLP  — runtime-swappable kernel (spell type changes physics), ~1.2KB weight buffer
//         Use MLP when the kernel must change during gameplay; LUT otherwise.
//
// WGPU compatibility: float16_t arithmetic, [ForceUnroll], no CoopVec extensions.

// ---------------------------------------------------------------------------
// Phase reaction: reaction(phi, temp) — 2-input [2→32→32→1]
// ---------------------------------------------------------------------------

// Weight buffer layout offsets for [2 → 32 → 32 → 1]:
//   W1: 0..63      (2×32 = 64 floats)
//   b1: 64..95     (32)
//   W2: 96..1119   (32×32 = 1024)
//   b2: 1120..1151 (32)
//   W3: 1152..1183 (1×32 = 32)
//   b3: 1184       (1)
//   Total: 1185 float16 values = 2.37 KB

static const int PHASE_W1_OFF  = 0;
static const int PHASE_B1_OFF  = 64;
static const int PHASE_W2_OFF  = 96;
static const int PHASE_B2_OFF  = 1120;
static const int PHASE_W3_OFF  = 1152;
static const int PHASE_B3_OFF  = 1184;

float neural_reaction_phase(
    StructuredBuffer<float16_t> gWeights,
    float phi,
    float temp)
{
    // Layer 1: [2 → 32]
    float h1[32];
    [ForceUnroll] for (int j = 0; j < 32; j++) {
        float acc = (float)gWeights[PHASE_B1_OFF + j];
        [ForceUnroll] for (int i = 0; i < 2; i++) {
            float x_i = (i == 0) ? phi : temp;
            acc += x_i * (float)gWeights[PHASE_W1_OFF + j * 2 + i];
        }
        h1[j] = max(0.0f, acc);  // ReLU
    }

    // Layer 2: [32 → 32]
    float h2[32];
    [ForceUnroll] for (int j = 0; j < 32; j++) {
        float acc = (float)gWeights[PHASE_B2_OFF + j];
        [ForceUnroll] for (int i = 0; i < 32; i++) {
            acc += h1[i] * (float)gWeights[PHASE_W2_OFF + j * 32 + i];
        }
        h2[j] = max(0.0f, acc);  // ReLU
    }

    // Layer 3: [32 → 1]
    float out = (float)gWeights[PHASE_B3_OFF];
    [ForceUnroll] for (int i = 0; i < 32; i++) {
        out += h2[i] * (float)gWeights[PHASE_W3_OFF + i];
    }
    return out;
}


// ---------------------------------------------------------------------------
// Latent reaction: reaction(phi, temp, lap_T) — 3-input [3→32→32→1]
// ---------------------------------------------------------------------------

// Weight buffer layout for [3 → 32 → 32 → 1]:
//   W1: 0..95      (3×32 = 96)
//   b1: 96..127    (32)
//   W2: 128..1151  (32×32 = 1024)
//   b2: 1152..1183 (32)
//   W3: 1184..1215 (1×32 = 32)
//   b3: 1216       (1)
//   Total: 1217 float16 values = 2.44 KB

static const int LATENT_W1_OFF  = 0;
static const int LATENT_B1_OFF  = 96;
static const int LATENT_W2_OFF  = 128;
static const int LATENT_B2_OFF  = 1152;
static const int LATENT_W3_OFF  = 1184;
static const int LATENT_B3_OFF  = 1216;

// lap_T normalization: training used lap_T ∈ [-8, 8] → normalize to [0, 1]
static const float LAPT_MIN = -8.0f;
static const float LAPT_MAX =  8.0f;

float neural_reaction_latent(
    StructuredBuffer<float16_t> gWeightsLatent,
    float phi,
    float temp,
    float lap_T)
{
    float lap_T_norm = clamp((lap_T - LAPT_MIN) / (LAPT_MAX - LAPT_MIN), 0.0f, 1.0f);

    // Layer 1: [3 → 32]
    float h1[32];
    [ForceUnroll] for (int j = 0; j < 32; j++) {
        float acc = (float)gWeightsLatent[LATENT_B1_OFF + j];
        float x[3] = { phi, temp, lap_T_norm };
        [ForceUnroll] for (int i = 0; i < 3; i++) {
            acc += x[i] * (float)gWeightsLatent[LATENT_W1_OFF + j * 3 + i];
        }
        h1[j] = max(0.0f, acc);
    }

    // Layer 2: [32 → 32]
    float h2[32];
    [ForceUnroll] for (int j = 0; j < 32; j++) {
        float acc = (float)gWeightsLatent[LATENT_B2_OFF + j];
        [ForceUnroll] for (int i = 0; i < 32; i++) {
            acc += h1[i] * (float)gWeightsLatent[LATENT_W2_OFF + j * 32 + i];
        }
        h2[j] = max(0.0f, acc);
    }

    // Layer 3: [32 → 1]
    float out = (float)gWeightsLatent[LATENT_B3_OFF];
    [ForceUnroll] for (int i = 0; i < 32; i++) {
        out += h2[i] * (float)gWeightsLatent[LATENT_W3_OFF + i];
    }
    return out;
}


// ---------------------------------------------------------------------------
// Usage example (in a compute shader):
// ---------------------------------------------------------------------------
//
// StructuredBuffer<float16_t> gPhaseWeights;    // 1185 float16 values
// StructuredBuffer<float16_t> gLatentWeights;   // 1217 float16 values
// Texture2D<float> ReactionLUT;                 // fallback baked LUT
// SamplerState LinearSampler;
//
// [numthreads(8, 8, 1)]
// void PhaseFieldCS(uint2 tid : SV_DispatchThreadID) {
//     float phi  = PhiBuffer[tid];
//     float temp = TempField[tid];
//
//     // Option A: baked LUT (best for fixed kernels, zero inference compute)
//     float reaction = ReactionLUT.SampleLevel(LinearSampler, float2(phi, temp), 0);
//
//     // Option B: MLP (use when kernel must change at spell-cast time)
//     // float reaction = neural_reaction_phase(gPhaseWeights, phi, temp);
//
//     float lap_phi = ...; // 5-point Laplacian
//     float dphi = dt * (D * lap_phi + reaction);
//     PhiBuffer[tid] = phi + dphi;
// }
