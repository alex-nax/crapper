#include "calc.h"

int main(void) {
  if (covered(3, 1) != 2) return 1;
  if (covered(1, 3) != 2) return 1;
  return 0;
}
