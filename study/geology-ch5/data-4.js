(function () {
  var d = window.STUDY_DATA;
  if (!d || !d.questions) throw new Error('data-4.js: load data.js first');
  d.questions.push(
    {
      n: 18,
      type: "mc",
      prompt: "In elastic rebound theory, what must occur to produce an earthquake?",
      choices: ["Enough stress must build up over time on a fault to overcome friction.", "Rocks must break in order to produce slip on a fault.", "The ground needs to absorb enough water to lubricate the surface of the fault.", "Tidal forces need to be at a maximum to increase the energy applied to the fault."],
      answer: 0,
      explanation: "Stress builds up over time until it overcomes friction on the fault. Rocks bend elastically, then the fault slips and they snap back, releasing the stored energy.",
      concepts: ["elastic-rebound"],
      verify: false
    },
    {
      n: 19,
      type: "mc",
      prompt: "The position on Earth's surface directly above the earthquake source is called the ________.",
      choices: ["epicenter", "inertial point", "focus", "seismic zone"],
      answer: 0,
      explanation: "The epicenter is the point on Earth's surface directly above the earthquake source. The source itself, underground, is the focus.",
      concepts: ["epicenter", "focus"],
      verify: false
    },
    {
      n: 20,
      type: "tf",
      prompt: "S waves can travel through solid and liquid media.",
      choices: ["True", "False"],
      answer: 1,
      explanation: "S waves travel only through solids. Their absence beyond about 104 degrees from a quake (the S-wave shadow zone) shows that the outer core is liquid.",
      concepts: ["s-waves", "interior-layers"],
      verify: false
    },
    {
      n: 21,
      type: "mc",
      prompt: "Oceanic crust is composed primarily of ________.",
      choices: ["granite", "basalt", "sandstone", "andesite"],
      answer: 1,
      explanation: "Oceanic crust is composed primarily of basalt, while continental crust is mostly granitic.",
      concepts: ["interior-layers"],
      verify: false
    },
    {
      n: 22,
      type: "mc",
      prompt: "Short term earthquake prediction research has focused on ________.",
      choices: ["looking at when and where earthquakes have occurred in the past to develop a model for the timing of future earthquakes", "measuring the rate of motion on various faults and the strength of the rocks along the faults to determine when the stress buildup will exceed the strength of the rocks", "looking for precursors such as changes in topography, animal behavior, foreshocks, radon emission, etc that might signal an imminent earthquake", "measuring vertical changes in elevation along faults to determine whether a fault zone is inflating before a major rupture"],
      answer: 2,
      explanation: "Short-term prediction research looks for precursors such as changes in topography, animal behavior, foreshocks and radon emission. No reliable short-term method exists yet.",
      concepts: ["prediction"],
      verify: false
    },
    {
      n: 23,
      type: "mc",
      prompt: "Long term earthquake prediction is based on ________.",
      choices: ["observations of things like animal behavior, radon gas emissions, and changes in topography", "the assumption that large faults break in a cyclical manner", "measurements of the plate motions and rate of stress accumulation", "measurements of strain accumulation"],
      answer: 1,
      explanation: "Long-term prediction assumes that large faults break in a cyclical manner. Long-range forecasts use the history of past large quakes (recurrence intervals, seismic gaps) to give probabilities over decades.",
      concepts: ["prediction"],
      verify: true
    },
    {
      n: 24,
      type: "mc",
      prompt: "The best building material to use in an earthquake prone region is ________.",
      choices: ["brick", "concrete", "wood", "stone"],
      answer: 2,
      explanation: "Wood frames are light and flexible. Unreinforced brick, stone and concrete are brittle and heavy.",
      concepts: ["building"],
      verify: false
    },
    {
      n: 25,
      type: "mc",
      prompt: "During the 1906 San Francisco earthquake, the movement of the Pacific Plate was approximately ________.",
      choices: ["1 cm", "10 cm", "1 meter", "10 meters", "100 meters"],
      answer: 3,
      explanation: "The San Andreas fault slipped horizontally by up to about 5-6 m (often quoted as 4.7 m) in 1906. Of these order-of-magnitude choices, the instructor's key may be 1 m or 10 m, so confirm this answer against the graded quiz.",
      concepts: ["san-andreas-1906"],
      verify: true
    }
  );
})();
