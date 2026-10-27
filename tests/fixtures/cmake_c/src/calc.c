#include "calc.h"

int covered(int a, int b) {
  if (a > b) {
    return a - b;
  }
  return b - a;
}

int uncovered(int a, int b) {
  if (a > b) {
    return a * b;
  }
  return a + b;
}
