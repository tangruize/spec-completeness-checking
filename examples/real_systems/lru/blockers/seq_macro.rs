use vstd::prelude::*;

verus! {

#[verifier::external_body]
pub fn singleton(key: u64) -> (result: Ghost<Seq<u64>>)
    ensures result@ == seq![key],
{
    unimplemented!()
}

}

fn main() {}
