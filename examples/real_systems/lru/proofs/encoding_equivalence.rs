use vstd::prelude::*;

verus! {

proof fn singleton_encoding_equivalent(key: u64)
    ensures
        seq![key] == Seq::<u64>::empty().push(key),
{
}

}

fn main() {}
