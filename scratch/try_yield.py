def gen():
    print("before yield")
    yield 1
    print("after yield 1")
    yield 2
    print("after yield 2")

g = gen()
print(next(g))
print(next(g))
print(next(g))