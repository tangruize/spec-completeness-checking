use vstd::prelude::*;

verus! {
    pub struct Counter {
        pub observed: u64,
        pub cache: u64,
    }

    impl View for Counter {
        type V = u64;
        open spec fn view(&self) -> u64 { self.observed }
    }

    impl Counter {
        pub fn value(&self) -> (result: u64)
            ensures result == self@,
        {
            self.observed
        }

        pub fn cached_value(&self) -> (result: u64)
            ensures result == self.cache,
        {
            self.cache
        }

        pub fn set_value(&mut self, value: u64)
            ensures
                final(self)@ == value,
                final(self).cache == old(self).cache,
        {
            self.observed = value;
        }
    }

    pub fn select(first: &Counter, second: &Counter, take_first: bool) -> (result: u64)
        requires first@ <= second@,
        ensures result == if take_first { first@ } else { second@ },
    {
        if take_first { first.observed } else { second.observed }
    }
}

fn main() {}

