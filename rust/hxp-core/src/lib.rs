//! Native core for 好想跑.
//!
//! Kept deliberately narrow: the NetSecKit request/response envelope and the
//! request-signing primitives.  The Python UI calls this through a small
//! line-oriented sidecar (`hxp-core.exe`), so the salts and the envelope
//! construction no longer ship as readable `.py`/`.pyc` code.

pub mod device;
pub mod gt4;
pub mod netsec;
pub mod obs;
pub mod pyval;
pub mod rng;
pub mod runcore;
pub mod rundata;
pub mod shield;
pub mod sign;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
