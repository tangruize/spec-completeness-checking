use vstd::prelude::*;

verus! {

#[verifier::external_body]
pub fn missing_return(slot: &mut u64) -> (result: bool)
    ensures *final(slot) == *old(slot),
{
    unimplemented!()
}

}

fn main() {}
