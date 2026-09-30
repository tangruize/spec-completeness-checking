use vstd::prelude::*;

verus! {

#[verifier::external_body]
pub fn missing_return(before: Ghost<Map<u64, u64>>) -> (result: bool)
    requires before@.dom().finite(),
    ensures true,
{
    unimplemented!()
}

}

fn main() {}
