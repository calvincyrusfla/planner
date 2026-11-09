// Chapter 5 study guide data: concepts and questions 1-2.
// Questions 3-25 are in data-2.js, data-3.js and data-4.js.
// Plain classic script so the guide works when opened from file://.
window.STUDY_DATA = {
  id: "geology-ch5",
  title: "Chapter 5 Study Guide: Earthquakes and Earth's Interior",
  source: "Quiz 9 - Chapter 5 Quiz",
  concepts: [
    {
      id: "elastic-rebound",
      term: "Elastic rebound theory",
      definition: "Rocks on either side of a fault slowly bend and store elastic strain as they are pushed in opposite directions. When the strain overcomes friction on the fault, the rocks slip and snap back toward their original shape, releasing the stored energy as seismic waves.",
      details: [
        "Proposed by H. F. Reid after studying ground displacement from the 1906 San Francisco earthquake.",
        "Think of bending a stick until it breaks: energy builds slowly and is released suddenly.",
        "The cycle repeats on active faults: strain builds, the fault ruptures, and strain begins to build again."
      ]
    },
    {
      id: "focus",
      term: "Focus (hypocenter)",
      definition: "The point within Earth where fault rupture begins and from which seismic waves first radiate. It is also called the hypocenter.",
      details: [
        "Focus depth is grouped as shallow (0-70 km), intermediate (70-300 km) or deep (300-700 km).",
        "Shallow-focus earthquakes usually cause the strongest shaking at the surface.",
        "The focus is underground; do not confuse it with the epicenter on the surface."
      ]
    },
    {
      id: "epicenter",
      term: "Epicenter",
      definition: "The point on Earth's surface directly above the focus. It is the location usually given in earthquake reports.",
      details: [
        "Shaking is generally strongest near the epicenter and weakens with distance.",
        "Its position is found by comparing wave arrival times at several seismic stations."
      ]
    },
    {
      id: "seismograph",
      term: "Seismograph and seismogram",
      definition: "A seismograph is the instrument that detects and records ground motion. A seismogram is the record it produces.",
      details: [
        "A suspended mass tends to stay still through inertia while the ground and the instrument frame move around it.",
        "A typical seismogram shows P waves arriving first, then S waves, then surface waves.",
        "Surface waves usually have the highest amplitudes on the record."
      ]
    },
    {
      id: "p-waves",
      term: "P waves",
      definition: "Primary body waves that travel through Earth's interior by compressing and expanding material in the direction of travel. They are the fastest seismic waves and the first to arrive at a station.",
      details: [
        "They pass through solids, liquids and gases.",
        "Particle motion is back and forth, parallel to the direction the wave travels.",
        "Also called compressional waves."
      ]
    },
    {
      id: "s-waves",
      term: "S waves",
      definition: "Secondary body waves that shear material from side to side, perpendicular to the direction of travel. They travel only through solids and move about 1.7 times slower than P waves.",
      details: [
        "Liquids and gases cannot resist shear, so S waves cannot pass through them.",
        "They arrive after P waves but before surface waves.",
        "Their failure to cross the outer core is key evidence that the outer core is liquid."
      ]
    },
    {
      id: "surface-waves",
      term: "Surface waves",
      definition: "Seismic waves that travel along Earth's surface rather than through its interior. They are the slowest waves, have the largest amplitudes and cause the most damage.",
      details: [
        "They arrive last on a seismogram and show the highest amplitudes.",
        "They move the ground both up and down and from side to side.",
        "Their energy stays near the surface, so they produce much of the shaking that damages buildings."
      ]
    },
    {
      id: "locating",
      term: "Locating an epicenter",
      definition: "The difference between P and S wave arrival times at a station gives the distance to the epicenter, because the gap grows with distance. Distances from three stations pinpoint the location.",
      details: [
        "A travel-time graph converts the S-minus-P time into a distance.",
        "Each station's distance defines a circle around it on a map.",
        "The three circles intersect at the epicenter, a method called triangulation.",
        "One station gives distance only, not direction."
      ]
    },
    {
      id: "intensity",
      term: "Intensity and the Modified Mercalli scale",
      definition: "Intensity measures how strongly an earthquake is felt and how much damage it causes at a particular place. The Modified Mercalli scale rates observed effects from I (not felt) to XII (near-total destruction).",
      details: [
        "One earthquake has many intensity values, which differ from place to place.",
        "Intensity depends on distance, focus depth, local ground conditions and building quality.",
        "It is based on observations, not on instrument readings."
      ]
    },
    {
      id: "richter",
      term: "Richter magnitude",
      definition: "A logarithmic measure of earthquake size based on the amplitude of the largest wave on a seismogram, adjusted for distance. Each whole-number step means 10 times the wave amplitude and about 32 times the energy.",
      details: [
        "A magnitude 7 has 100 times the amplitude of a magnitude 5 and about 1000 times the energy (32 x 32).",
        "An earthquake has one magnitude, unlike intensity, which varies by location.",
        "It underestimates the size of very large earthquakes."
      ]
    },
    {
      id: "moment-magnitude",
      term: "Moment magnitude",
      definition: "A measure of the total energy released by an earthquake, calculated from the fault area that ruptured, the average slip and the rigidity of the rock. It is the preferred scale for large earthquakes.",
      details: [
        "Seismic moment = rock rigidity x fault area x average slip.",
        "It stays accurate for very large events where the Richter scale falls short.",
        "Like the Richter scale, each whole number means about 32 times more energy."
      ]
    },
    {
      id: "megathrust-tsunami",
      term: "Megathrust faults and tsunamis",
      definition: "Megathrust faults are the huge plate-boundary faults of subduction zones and produce Earth's largest earthquakes. Sudden displacement of the seafloor during rupture lifts the overlying water and can generate a tsunami.",
      details: [
        "In deep water a tsunami travels about as fast as a jet airliner but may be low and hard to notice.",
        "The wave slows and grows much taller as it reaches shallow water near shore.",
        "Warning systems use seismic data, tide gauges and ocean sensors to alert coasts before waves arrive.",
        "Examples include Chile 1960, Alaska 1964, Sumatra 2004 and Japan 2011."
      ]
    },
    {
      id: "liquefaction-landslides",
      term: "Ground shaking effects: liquefaction and landslides",
      definition: "Shaking can make loose, water-saturated sediment lose its strength and behave like a fluid, which is called liquefaction. Shaking can also trigger landslides on slopes.",
      details: [
        "Liquefied ground can make buildings tilt or sink and can make buried tanks and pipes float upward.",
        "At Turnagain Heights in Anchorage in 1964, a weak clay layer failed and homes slid toward the sea.",
        "Damage is often worse on soft sediment or fill than on solid bedrock."
      ]
    },
    {
      id: "building",
      term: "Building materials and codes",
      definition: "How well a structure survives shaking depends strongly on its materials and design. Building codes require construction that can withstand the shaking expected in a region.",
      details: [
        "Wood-frame buildings are flexible and generally perform well.",
        "Unreinforced masonry is brittle and prone to collapse.",
        "Base isolation places flexible bearings between a building and its foundation to reduce the shaking that reaches the structure."
      ]
    },
    {
      id: "plate-boundaries",
      term: "Earthquake belts and plate boundaries",
      definition: "Most earthquakes occur in narrow belts along plate boundaries. The main belts are the circum-Pacific belt, the Alpine-Himalayan (Mediterranean) belt and the mid-ocean ridges.",
      details: [
        "The circum-Pacific belt follows the subduction zones and transform faults around the Pacific and releases most of the world's seismic energy.",
        "The Alpine-Himalayan belt includes the Mediterranean region, where Africa converges with Eurasia.",
        "Mid-ocean ridges have frequent, shallow and mostly smaller earthquakes."
      ]
    },
    {
      id: "san-andreas-1906",
      term: "1906 San Francisco earthquake",
      definition: "A major 1906 earthquake on the San Andreas Fault, a transform boundary between the Pacific and North American plates. Horizontal slip reached roughly 4.7 to 6 meters, and fires caused much of the destruction in the city.",
      details: [
        "Motion on a transform fault is mainly horizontal.",
        "Broken water mains made the fires that followed very hard to fight.",
        "Measurements of the offset ground led to elastic rebound theory."
      ]
    },
    {
      id: "prediction",
      term: "Earthquake prediction and forecasting",
      definition: "Short-term prediction tries to give the time and place of a specific earthquake using precursors and is not yet reliable. Long-range forecasting estimates the probability of an earthquake on a fault over decades.",
      details: [
        "Proposed precursors include changes in ground tilt, groundwater, radon gas and small foreshocks, but none works consistently.",
        "Forecasts rely on recurrence intervals, the average time between past earthquakes on a fault.",
        "A seismic gap is a quiet segment of an active fault that may be storing strain for a future rupture.",
        "Forecasts are stated as probabilities, such as a percent chance within 30 years."
      ]
    },
    {
      id: "early-earth",
      term: "Early Earth heating and differentiation",
      definition: "The young Earth became hot enough for iron and other materials to melt. Dense iron and nickel sank to form the core while lighter silicates rose, separating Earth into layers.",
      details: [
        "Heat came from colliding debris, gravitational compression and radioactive decay.",
        "This sorting of materials by density is called chemical differentiation.",
        "Differentiation produced the layered structure of core, mantle and crust."
      ]
    },
    {
      id: "interior-layers",
      term: "Earth's interior layers",
      definition: "Earth is layered into a thin rocky crust, a thick solid-rock mantle, a liquid outer core and a solid inner core. The cores are made mostly of iron and nickel.",
      details: [
        "Oceanic crust is thin and basaltic; continental crust is thicker and broadly granitic.",
        "The Moho (Mohorovicic discontinuity) is the boundary between the crust and the mantle, where seismic velocity jumps.",
        "The mantle extends to about 2900 km depth and is the largest layer by volume.",
        "The inner core is solid despite its high temperature because of the enormous pressure."
      ]
    },
    {
      id: "wave-velocity-depth",
      term: "Seismic velocity, refraction and shadow zones",
      definition: "Seismic velocity generally increases with depth, so wave paths refract and curve back toward the surface. Waves also bend or stop at major boundaries, leaving shadow zones where direct waves are not recorded.",
      details: [
        "Refraction is the bending of a wave as its speed changes.",
        "S waves cannot cross the liquid outer core, so a large S-wave shadow zone lies on the side of Earth opposite the earthquake.",
        "P waves are refracted at the core-mantle boundary, creating a narrower P-wave shadow band.",
        "Shadow zones revealed the size of the core and showed that the outer core is liquid."
      ]
    }
  ],
  questions: [
    {
      n: 1,
      type: "mc",
      prompt: "On a typical seismogram, ________ will show the highest amplitudes.",
      choices: ["P waves", "S waves", "surface waves", "body waves"],
      answer: 2,
      explanation: "Surface waves travel along Earth's surface and arrive last on a seismogram. They have the largest amplitudes and cause the most shaking.",
      concepts: ["surface-waves", "seismograph"],
      verify: false
    },
    {
      n: 2,
      type: "mc",
      prompt: "Which one of the following statements is correct?",
      choices: [
        "P waves travel through solids; S waves do not.",
        "P and S waves travel through liquids, but P waves do not travel through solids.",
        "P and S waves travel through solids and P waves travel through liquids.",
        "P and S waves travel through liquids, but S waves do not travel through solids."
      ],
      answer: 2,
      explanation: "P (compressional) waves pass through solids, liquids and gases. S (shear) waves pass only through solids, because liquids cannot resist shear.",
      concepts: ["p-waves", "s-waves"],
      verify: false
    }
  ]
};
