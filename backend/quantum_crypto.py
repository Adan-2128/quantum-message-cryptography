"""
quantum_crypto.py
------------------
BB84 quantum key distribution + Pauli one-time-pad encryption, built on
Qiskit. This is the crypto engine the chat server calls into.

DEFAULT MODE: runs on a local Qiskit Aer simulator -- NO API key, NO
internet account, NO IBM Quantum access required. This is what makes
the chat app respond instantly instead of waiting in a hardware queue.

REAL HARDWARE MODE: set USE_REAL_HARDWARE = True below to run BB84 key
*generation* (only -- see "Why only BB84, not message encryption" below)
on a real IBM quantum computer. Requires a free IBM Quantum account + a
saved API token (run the one-time QiskitRuntimeService.save_account(...)
step separately, in a REPL -- never hard-code a token in this file).
This is wired in directly here, in the exact same function (bb84_generate_key)
that app.py already calls -- app.py itself needs NO changes to use real
hardware; flipping this one flag is the only required edit.

Why only BB84, not message encryption, runs on real hardware:
queueing a real QPU job for every keystroke would make the chat
unusable (queue times range from seconds to hours). BB84 only runs
occasionally, to refill the room's key pool, so an occasional real
hardware wait is tolerable; per-message encryption stays on the local
simulator so the chat itself always feels instant.

Honest technical note (matters for your presentation): plaintext bits
here are encoded as definite classical states, not genuine quantum
superpositions, so the Pauli-Z half of the one-time-pad mask has no
observable effect on the recovered bit (verified numerically -- see the
chat with Claude where this was checked). The X mask alone provides the
actual secrecy, functioning as a quantum-key-distributed classical
XOR one-time pad. The BB84 key-generation layer is unaffected by this
and remains the real, physically-grounded part of the system.
"""

import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import Statevector

USE_REAL_HARDWARE = True
  # flip to True only if you have IBM Quantum access
                           # (one-time setup: see IBM_Quantum_Hardware_Setup_Guide.docx)

# Real-hardware backend/sampler are created lazily (only if actually used),
# so nobody running the default simulator path needs qiskit-ibm-runtime
# installed at all.
_hw_backend = None
_hw_sampler = None


def _get_hardware_backend():
    global _hw_backend, _hw_sampler
    if _hw_backend is None:
        from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2
        service = QiskitRuntimeService()  # reads your saved account credentials
        _hw_backend = service.least_busy(operational=True, simulator=False)
        _hw_sampler = SamplerV2(mode=_hw_backend)
        print(f"[quantum_crypto] Using real IBM backend: {_hw_backend.name}")
    return _hw_backend, _hw_sampler


# ============================================================
# BB84 Quantum Key Distribution
# ============================================================

def _bb84_prepare_qubit(bit: int, basis: int) -> QuantumCircuit:
    """Simulator path: prepare only (state read out directly, no measurement)."""
    qc = QuantumCircuit(1)
    if bit == 1:
        qc.x(0)
    if basis == 1:
        qc.h(0)
    return qc


def _bb84_measure(state: Statevector, basis: int, rng: np.random.Generator) -> int:
    """Simulator path: measure by sampling from exact Born-rule probabilities."""
    qc = QuantumCircuit(1)
    if basis == 1:
        qc.h(0)
    measured = state.evolve(qc)
    probs = measured.probabilities()
    return int(rng.choice([0, 1], p=probs))


def _bb84_prepare_and_measure_circuit(bit: int, prep_basis: int, meas_basis: int) -> QuantumCircuit:
    """Hardware path: one qubit's full journey (prepare + measure) as one circuit,
    since real qubits can only be measured, never read out as amplitudes."""
    qc = QuantumCircuit(1, 1)
    if bit == 1:
        qc.x(0)
    if prep_basis == 1:
        qc.h(0)
    if meas_basis == 1:
        qc.h(0)
    qc.measure(0, 0)
    return qc


def _run_hardware_batch(circuits: list) -> list:
    """Hardware path: transpile + submit many 1-qubit circuits as ONE job, 1 shot each."""
    from qiskit import transpile
    backend, sampler = _get_hardware_backend()
    transpiled = transpile(circuits, backend=backend, optimization_level=1)
    job = sampler.run(transpiled, shots=1)
    result = job.result()
    outcomes = []
    for i in range(len(circuits)):
        counts = result[i].data.c.get_counts()
        outcomes.append(int(list(counts.keys())[0]))
    return outcomes


def bb84_generate_key(n_raw_bits: int, sample_fraction: float = 0.3, seed=None) -> dict:
    """
    Runs BB84 between two logical parties and returns a dict with the
    final shared secret key plus the error-rate stats used to detect
    eavesdropping.

    Dispatches on the module-level USE_REAL_HARDWARE flag:
      - False (default): Qiskit Statevector simulation -- instant, exact.
      - True: real circuits submitted as a batched job to a real IBM
        quantum processor via Qiskit Runtime -- slower (queue-dependent),
        and will show a small nonzero QBER even with no eavesdropper,
        due to genuine hardware noise.

    app.py calls this exact function either way and needs no changes.
    """
    rng = np.random.default_rng(seed)

    alice_bits = rng.integers(0, 2, n_raw_bits).tolist()
    alice_bases = rng.integers(0, 2, n_raw_bits).tolist()
    bob_bases = rng.integers(0, 2, n_raw_bits).tolist()

    if USE_REAL_HARDWARE:
        circuits = [_bb84_prepare_and_measure_circuit(b, ab, bb)
                    for b, ab, bb in zip(alice_bits, alice_bases, bob_bases)]
        bob_results = _run_hardware_batch(circuits)
    else:
        sent_states = [Statevector.from_instruction(_bb84_prepare_qubit(b, ba))
                       for b, ba in zip(alice_bits, alice_bases)]
        bob_results = [_bb84_measure(s, bb, rng) for s, bb in zip(sent_states, bob_bases)]

    sifted_a, sifted_b = [], []
    for ab, aba, bb_, br in zip(alice_bits, alice_bases, bob_bases, bob_results):
        if aba == bb_:
            sifted_a.append(ab)
            sifted_b.append(br)

    n_sift = len(sifted_a)
    n_sample = max(1, int(n_sift * sample_fraction)) if n_sift else 0
    sample_idx = set(rng.choice(n_sift, size=n_sample, replace=False)) if n_sift else set()
    mismatches = sum(1 for i in sample_idx if sifted_a[i] != sifted_b[i])
    qber = mismatches / n_sample if n_sample else 0.0

    final_key = [sifted_a[i] for i in range(n_sift) if i not in sample_idx]
    # Slightly above the textbook 0.11 to allow margin for real hardware's natural noise floor
    threshold = 0.15 if USE_REAL_HARDWARE else 0.11

    return {
        "key": final_key,
        "qber": qber,
        "eavesdropper_detected": qber > threshold,
        "final_key_length": len(final_key),
    }


# ============================================================
# One-time-pad message encryption (quantum-key-distributed)
# ============================================================

def text_to_bits(text: str) -> list:
    return [int(b) for char in text for b in format(ord(char), "08b")]


def bits_to_text(bits: list) -> str:
    chars = []
    for i in range(0, len(bits), 8):
        byte = bits[i:i + 8]
        if len(byte) < 8:
            break
        char_code = int("".join(map(str, byte)), 2)
        chars.append(chr(char_code))
    return "".join(chars)


def _int_to_qubit_ordered_bits(value: int, n: int) -> list:
    return [int(b) for b in format(value, f"0{n}b")[::-1]]


def otp_encrypt(plaintext: str, key_bits: list):
    """
    Encrypts text by applying a secret X mask (bit flip) per qubit,
    keyed by key_bits. Requires len(key_bits) >= len(plaintext)*8.
    Returns (ciphertext_bits, n_qubits) -- ciphertext_bits is what
    would travel over the wire.
    """
    plain_bits = text_to_bits(plaintext)
    n = len(plain_bits)
    if len(key_bits) < n:
        raise ValueError(f"Need {n} key bits, have {len(key_bits)}")

    qc = QuantumCircuit(n)
    for i, bit in enumerate(plain_bits):
        if bit == 1:
            qc.x(i)
    for i in range(n):
        if key_bits[i] == 1:
            qc.x(i)
    state = Statevector.from_instruction(qc)
    idx = int(np.argmax(state.probabilities()))
    ciphertext_bits = _int_to_qubit_ordered_bits(idx, n)
    return ciphertext_bits, n


def otp_decrypt(ciphertext_bits: list, key_bits: list) -> str:
    """Applies the same X mask again (self-inverse) to recover plaintext."""
    n = len(ciphertext_bits)
    qc = QuantumCircuit(n)
    for i, bit in enumerate(ciphertext_bits):
        if bit == 1:
            qc.x(i)
    for i in range(n):
        if key_bits[i] == 1:
            qc.x(i)
    state = Statevector.from_instruction(qc)
    idx = int(np.argmax(state.probabilities()))
    recovered_bits = _int_to_qubit_ordered_bits(idx, n)
    return bits_to_text(recovered_bits)


CHARS_PER_BLOCK = 2  # 2 chars = 16 qubits per block -- fast and well under Qiskit's limit


def otp_encrypt_message(plaintext: str, key_bits: list, chars_per_block: int = CHARS_PER_BLOCK) -> list:
    """
    FIX: chat messages are often long enough that encrypting the whole
    thing as one circuit (one qubit per bit) blows past Qiskit's
    statevector qubit limit -- a 15-character message is already 120
    qubits, which simply crashes. This splits the message into small
    blocks, encrypts each independently, and concatenates the
    ciphertext bits. Messages of any length now work.
    """
    all_cipher_bits = []
    key_cursor = 0
    for i in range(0, len(plaintext), chars_per_block):
        block = plaintext[i:i + chars_per_block]
        n_bits = len(block) * 8
        block_key = key_bits[key_cursor: key_cursor + n_bits]
        key_cursor += n_bits
        cbits, _ = otp_encrypt(block, block_key)
        all_cipher_bits.extend(cbits)
    return all_cipher_bits


def otp_decrypt_message(ciphertext_bits: list, key_bits: list, chars_per_block: int = CHARS_PER_BLOCK) -> str:
    """Inverse of otp_encrypt_message -- decrypts block by block and joins the text."""
    bits_per_block = chars_per_block * 8
    parts = []
    for i in range(0, len(ciphertext_bits), bits_per_block):
        cblock = ciphertext_bits[i:i + bits_per_block]
        kblock = key_bits[i:i + bits_per_block]
        parts.append(otp_decrypt(cblock, kblock))
    return "".join(parts)


def bits_to_hex(bits: list) -> str:
    """For displaying ciphertext in the UI."""
    padded = bits + [0] * ((8 - len(bits) % 8) % 8)
    out = bytearray()
    for i in range(0, len(padded), 8):
        out.append(int("".join(map(str, padded[i:i + 8])), 2))
    return out.hex()