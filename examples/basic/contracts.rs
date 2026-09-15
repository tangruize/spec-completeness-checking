use vstd::prelude::*;

verus! {
    pub fn identity(value: bool) -> (result: bool)
        ensures result == value,
    {
        value
    }

    pub fn loose(value: bool) -> (result: bool)
        ensures true,
    {
        value
    }

    pub fn replace_value(slot: &mut u64, value: u64)
        ensures *final(slot) == value,
    {
        *slot = value;
    }
}

fn main() {}
