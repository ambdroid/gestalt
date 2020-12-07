// Created in OpenSCAD
N = 6;
BIGRADIUS = 400;
SMALLRADIUS = BIGRADIUS/3;

rotate(180/N) {
    difference() {
        for (i = [1:N])
            rotate(i*360/N)
                translate([BIGRADIUS, 0])
                    circle(SMALLRADIUS, $fn = 50);
        circle(BIGRADIUS, $fn = N);
    }
}