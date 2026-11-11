//! Deterministic PRNG for the native run core.
//!
//! Python seeds this with `random.Random.getrandbits(64)`, so a seeded caller
//! still gets a reproducible run.  The stream is deliberately not CPython's
//! MT19937: matching every `gauss`/`sample` draw would couple this code to an
//! implementation detail the tests do not depend on.

/// SplitMix64.
pub struct Rng {
    state: u64,
}

impl Rng {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in [0, 1), 53-bit resolution.
    pub fn random(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / 9_007_199_254_740_992.0)
    }

    pub fn uniform(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.random()
    }

    /// Box-Muller; one value per call, no hidden cache.
    pub fn gauss(&mut self, mean: f64, sigma: f64) -> f64 {
        let u1 = self.random().max(1e-12);
        let u2 = self.random();
        mean + sigma * (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }

    /// Inclusive integer range, like `random.randint`.
    pub fn randint(&mut self, low: i64, high: i64) -> i64 {
        let span = (high - low + 1).max(1) as u64;
        low + (self.next_u64() % span) as i64
    }

    /// Uniform index in `0..length`.
    pub fn below(&mut self, length: usize) -> usize {
        if length == 0 {
            return 0;
        }
        (self.next_u64() % length as u64) as usize
    }

    /// `k` distinct indices from `start..end`, like `random.sample(range(...))`.
    pub fn sample_range(&mut self, start: usize, end: usize, k: usize) -> Vec<usize> {
        let mut pool: Vec<usize> = (start..end).collect();
        let k = k.min(pool.len());
        for i in 0..k {
            let j = i + self.below(pool.len() - i);
            pool.swap(i, j);
        }
        pool.truncate(k);
        pool
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_seed_same_stream() {
        let mut a = Rng::new(7);
        let mut b = Rng::new(7);
        for _ in 0..10 {
            assert_eq!(a.next_u64(), b.next_u64());
        }
    }

    #[test]
    fn random_stays_in_range_and_ints_are_inclusive() {
        let mut rng = Rng::new(1);
        for _ in 0..200 {
            let value = rng.random();
            assert!((0.0..1.0).contains(&value));
            let n = rng.randint(2, 5);
            assert!((2..=5).contains(&n));
        }
    }

    #[test]
    fn sample_returns_distinct_indices() {
        let mut rng = Rng::new(3);
        let picks = rng.sample_range(2, 20, 4);
        assert_eq!(picks.len(), 4);
        let mut sorted = picks.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), 4);
        assert!(sorted.iter().all(|i| (2..20).contains(i)));
    }
}
