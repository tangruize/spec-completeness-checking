use lru::LruCache;
use std::collections::BTreeMap;
use std::num::NonZeroUsize;

#[derive(Clone, Debug, PartialEq, Eq)]
struct State {
    capacity: usize,
    entries: BTreeMap<u64, u64>,
    order: Vec<u64>,
}

impl State {
    fn snapshot(cache: &LruCache<u64, u64>) -> Self {
        Self {
            capacity: cache.cap().get(),
            entries: cache.iter().map(|(&k, &v)| (k, v)).collect(),
            order: cache.iter().map(|(&k, _)| k).collect(),
        }
    }

    fn check_wf(&self) {
        assert!(self.capacity > 0);
        assert!(self.order.len() <= self.capacity);
        assert_eq!(self.order.len(), self.entries.len());
        let keys: std::collections::BTreeSet<_> = self.order.iter().copied().collect();
        assert_eq!(keys.len(), self.order.len());
        assert_eq!(keys, self.entries.keys().copied().collect());
    }

    fn realize(&self) -> LruCache<u64, u64> {
        let mut cache = LruCache::new(NonZeroUsize::new(self.capacity).unwrap());
        for key in self.order.iter().rev() {
            cache.put(*key, self.entries[key]);
        }
        assert_eq!(Self::snapshot(&cache), *self);
        cache
    }
}

#[derive(Default)]
struct Counts {
    states: usize,
    put: usize,
    get: usize,
    pop: usize,
    put_update: usize,
    put_update_at_capacity: usize,
    put_insert_nonfull: usize,
    put_evict: usize,
    get_hit: usize,
    get_miss: usize,
    pop_hit: usize,
    pop_miss: usize,
}

fn check_state(before: &State, counts: &mut Counts) {
    before.check_wf();
    counts.states += 1;
    for key in 0..4 {
        for value in 0..3 {
            let mut real = before.realize();
            let returned = real.put(key, value);
            let after = State::snapshot(&real);
            let mut expected = before.clone();
            let old_value = before.entries.get(&key).copied();
            if old_value.is_some() {
                counts.put_update += 1;
                if before.order.len() == before.capacity {
                    counts.put_update_at_capacity += 1;
                }
                expected.order.retain(|item| *item != key);
            } else if before.order.len() == before.capacity {
                counts.put_evict += 1;
                let victim = expected.order.pop().unwrap();
                expected.entries.remove(&victim);
            } else {
                counts.put_insert_nonfull += 1;
            }
            expected.entries.insert(key, value);
            expected.order.insert(0, key);
            after.check_wf();
            assert_eq!(returned, old_value, "put return: {before:?}, key={key}");
            assert_eq!(after, expected, "put state: {before:?}, key={key}");
            counts.put += 1;
        }

        let mut real = before.realize();
        let returned = real.get(&key).copied();
        let after = State::snapshot(&real);
        let mut expected = before.clone();
        let old_value = before.entries.get(&key).copied();
        if old_value.is_some() {
            counts.get_hit += 1;
            expected.order.retain(|item| *item != key);
            expected.order.insert(0, key);
        } else {
            counts.get_miss += 1;
        }
        after.check_wf();
        assert_eq!(returned, old_value, "get return: {before:?}, key={key}");
        assert_eq!(after, expected, "get state: {before:?}, key={key}");
        counts.get += 1;

        let mut real = before.realize();
        let returned = real.pop(&key);
        let after = State::snapshot(&real);
        let mut expected = before.clone();
        expected.entries.remove(&key);
        expected.order.retain(|item| *item != key);
        if old_value.is_some() {
            counts.pop_hit += 1;
        } else {
            counts.pop_miss += 1;
        }
        after.check_wf();
        assert_eq!(returned, old_value, "pop return: {before:?}, key={key}");
        assert_eq!(after, expected, "pop state: {before:?}, key={key}");
        counts.pop += 1;
    }
}

fn enumerate(capacity: usize, ordered_entries: &mut Vec<(u64, u64)>, counts: &mut Counts) {
    let before = State {
        capacity,
        entries: ordered_entries.iter().copied().collect(),
        order: ordered_entries.iter().map(|(key, _)| *key).collect(),
    };
    check_state(&before, counts);
    if ordered_entries.len() == capacity {
        return;
    }
    for key in 0..4 {
        if ordered_entries.iter().any(|(present, _)| *present == key) {
            continue;
        }
        for value in 0..3 {
            ordered_entries.push((key, value));
            enumerate(capacity, ordered_entries, counts);
            ordered_entries.pop();
        }
    }
}

fn main() {
    let mut counts = Counts::default();
    for capacity in 1..=3 {
        enumerate(capacity, &mut Vec::new(), &mut counts);
    }
    assert_eq!(counts.states, 903);
    assert_eq!(counts.put + counts.get + counts.pop, 18_060);
    assert!(counts.put_update_at_capacity > 0);
    println!(
        "{{\"states\":{},\"transitions\":{},\"put\":{},\"get\":{},\"pop\":{},\
        \"put_update\":{},\"put_update_at_capacity\":{},\"put_insert_nonfull\":{},\
        \"put_evict\":{},\"get_hit\":{},\"get_miss\":{},\"pop_hit\":{},\"pop_miss\":{},\
        \"capacities\":[1,2,3],\"keys\":[0,1,2,3],\"values\":[0,1,2],\
        \"scope\":\"finite differential check against a hand-transcribed model; not a refinement proof\"}}",
        counts.states,
        counts.put + counts.get + counts.pop,
        counts.put,
        counts.get,
        counts.pop,
        counts.put_update,
        counts.put_update_at_capacity,
        counts.put_insert_nonfull,
        counts.put_evict,
        counts.get_hit,
        counts.get_miss,
        counts.pop_hit,
        counts.pop_miss,
    );
}
