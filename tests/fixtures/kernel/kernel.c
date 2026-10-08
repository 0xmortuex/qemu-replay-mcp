/* Test kernel for gdbstub-mcp's integration tests. Built with frame pointers
 * and -O0 so backtraces and line numbers are deterministic. */

volatile unsigned int counter;
volatile unsigned int trigger;   /* tests set this to 1 to force a triple fault */

struct __attribute__((packed)) idtr { unsigned short limit; unsigned int base; };

int add(int a, int b) {
    return a + b;               /* ADD_LINE */
}

int compute(int n) {
    int total = 0;
    for (int i = 0; i < n; i++)
        total = add(total, i);
    return total;
}

void triple_fault(void) {
    static const struct idtr empty = {0, 0};
    __asm__ volatile("lidt %0" : : "m"(empty));
    __asm__ volatile("ud2");    /* #UD with no IDT -> #GP -> #DF -> reset */
}

/* qemu-replay-mcp's copy adds this line (gdbstub-mcp's fixture has none) so
 * the until_serial recording stop can be tested on a kernel that never ends. */
static void serial_puts(const char *s) {
    for (; *s; s++)
        __asm__ volatile("outb %0, %1" : : "a"(*s), "Nd"((unsigned short)0x3F8));
}

void kmain(void) {
    serial_puts("loop ready\n");
    for (;;) {
        counter += (unsigned int)compute(5);
        if (trigger)
            triple_fault();
    }
}
